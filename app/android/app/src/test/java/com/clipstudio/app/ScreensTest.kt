package com.clipstudio.app

import android.content.Context
import android.graphics.Bitmap
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.hasSetTextAction
import androidx.test.core.app.ApplicationProvider
import androidx.work.testing.WorkManagerTestInitHelper
import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.ApiJson
import com.clipstudio.app.data.AppGraph
import com.clipstudio.app.data.Clip
import com.clipstudio.app.ui.ClipScreen
import com.clipstudio.app.ui.EditorScreen
import com.clipstudio.app.ui.ProjectScreen
import com.clipstudio.app.ui.ProjectsScreen
import com.clipstudio.app.ui.SettingsScreen
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList

/**
 * Telas do app renderizadas na JVM (Robolectric) contra um servidor falso que devolve respostas
 * capturadas da API real. Também grava capturas de tela em app/build/screenshots/.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w400dp-h860dp-xhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ScreensTest {
    @get:Rule val compose = createAndroidComposeRule<ComponentActivity>()

    private lateinit var server: MockWebServer
    private lateinit var api: ApiClient
    private val patches = CopyOnWriteArrayList<String>()

    private fun fixture(name: String) = javaClass.classLoader!!.getResource(name)!!.readText()
    private val clips by lazy { ApiJson.decodeFromString(ListSerializer(Clip.serializer()), fixture("clips.json")) }
    private val projectId by lazy { ApiJson.parseToJsonElement(fixture("project.json")).jsonObject["id"]!!.jsonPrimitive.content }

    @Before
    fun setUp() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        AppGraph.init(context)
        AppGraph.save("", "") // AppGraph é singleton: não deixa estado de um teste vazar para o outro
        server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.path!!.substringBefore('?')
                fun json(body: String) = MockResponse().setHeader("Content-Type", "application/json").setBody(body)
                return when {
                    path == "/health" -> json(fixture("health.json"))
                    path == "/auth/login" -> {
                        val body = request.body.readUtf8()
                        if (body.contains("senha-certa")) json("""{"token":"jwt-falso","user":{"id":"u1","email":"ana@ex.com","is_admin":false}}""")
                        else MockResponse().setResponseCode(401).setBody("""{"detail":"e-mail ou senha incorretos"}""")
                    }
                    path == "/projects" -> json("[" + fixture("project.json") + "]")
                    path == "/projects/$projectId" -> json(fixture("project.json"))
                    path == "/projects/$projectId/clips" -> json(fixture("clips.json"))
                    path == "/projects/$projectId/transcript.json" -> json(fixture("transcript.json"))
                    path.startsWith("/clips/") && path.endsWith("/file") -> MockResponse().setResponseCode(404)
                    path.startsWith("/clips/") && request.method == "PATCH" -> {
                        patches += request.body.readUtf8()
                        val c = clips.first { path.endsWith(it.id) }
                        json(ApiJson.encodeToString(Clip.serializer(), c.copy(version = c.version + 1, renderPending = true)))
                    }
                    path.startsWith("/clips/") -> {
                        val c = clips.first { path.endsWith(it.id) }
                        // prévia "desatualizada" evita abrir o player (ExoPlayer) na JVM
                        json(ApiJson.encodeToString(Clip.serializer(), c.copy(preview = c.preview?.copy(stale = true))))
                    }
                    else -> MockResponse().setResponseCode(404)
                }
            }
        }
        server.start()
        api = ApiClient(server.url("/").toString(), "token-local-teste")
    }

    @After
    fun tearDown() = server.shutdown()

    private fun show(content: @Composable () -> Unit) {
        // As telas fazem polling infinito (delay em loop): com o relógio automático o teste nunca fica "ocioso".
        compose.mainClock.autoAdvance = false
        compose.setContent { MaterialTheme(colorScheme = darkColorScheme()) { Surface { content() } } }
    }

    /** Avança o relógio do Compose em passos e dá tempo às chamadas de rede (threads reais) até a condição valer. */
    private fun waitFor(timeout: Long = 20_000, condition: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeout
        while (System.currentTimeMillis() < deadline) {
            compose.mainClock.advanceTimeBy(250)
            if (condition()) return
            Thread.sleep(50)
        }
        throw AssertionError("condição não satisfeita em ${timeout} ms")
    }

    private fun waitForText(text: String, timeout: Long = 20_000) = waitFor(timeout) {
        compose.onAllNodesWithText(text, substring = true, useUnmergedTree = true).fetchSemanticsNodes().isNotEmpty()
    }

    private fun screenshot(name: String) {
        compose.mainClock.advanceTimeBy(500) // termina animações
        val root = compose.activity.window.decorView
        val bmp = Bitmap.createBitmap(root.width, root.height, Bitmap.Config.ARGB_8888)
        root.draw(android.graphics.Canvas(bmp))
        val dir = File("build/screenshots").apply { mkdirs() }
        File(dir, "$name.png").outputStream().use { bmp.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test(timeout = 120_000)
    fun settingsLogsInWithAccount() {
        var done = false
        show { SettingsScreen(onDone = { done = true }) }
        compose.onNodeWithText("Conta").performClick()
        compose.mainClock.advanceTimeBy(300)
        val fields = compose.onAllNodes(hasSetTextAction())
        fields[0].performTextReplacement(server.url("/").toString().trimEnd('/'))
        fields[1].performTextReplacement("ana@ex.com")
        fields[2].performTextReplacement("senha-errada")
        compose.onNodeWithText("Entrar").performClick()
        waitForText("incorretos")
        screenshot("01_login")
        fields[2].performTextReplacement("senha-certa")
        compose.onNodeWithText("Entrar").performClick()
        waitFor { done }
        assertEquals("jwt-falso", AppGraph.token)
        assertEquals("ana@ex.com", AppGraph.accountEmail)
    }

    @Test(timeout = 120_000)
    fun settingsAdminTokenTestsConnection() {
        var done = false
        show { SettingsScreen(onDone = { done = true }) }
        compose.onNodeWithText("Token de administrador").performClick()
        compose.mainClock.advanceTimeBy(300) // relógio manual: recompõe após o clique
        val fields = compose.onAllNodes(hasSetTextAction())
        fields[0].performTextReplacement(server.url("/").toString().trimEnd('/'))
        fields[1].performTextReplacement("token-local-teste")
        compose.onNodeWithText("Testar conexão").performClick()
        waitForText("Conectado")
        screenshot("01b_token")
        compose.onNodeWithText("Salvar").performClick()
        assertTrue(done)
        assertEquals("", AppGraph.accountEmail)
    }

    @Test(timeout = 120_000)
    fun projectsListShowsServerProjects() {
        show { ProjectsScreen(api, onOpen = {}, onSettings = {}) }
        waitForText("teste_sintetico")
        compose.onNodeWithText("Pronto", substring = true).assertExists()
        compose.onNodeWithText("Novo vídeo", useUnmergedTree = true).assertExists()
        screenshot("02_projetos")
    }

    @Test(timeout = 120_000)
    fun projectScreenListsClipsWithStatus() {
        var opened: String? = null
        show { ProjectScreen(api, projectId, onClip = { opened = it }, onBack = {}) }
        waitForText("#1 · ")
        compose.onNodeWithText("Pronto").assertExists()
        for (rank in 1..3) compose.onNodeWithText("#$rank · ", substring = true).assertExists()
        screenshot("03_projeto")
        compose.onNodeWithText("#2 · ", substring = true).performClick()
        assertEquals(clips[1].id, opened)
    }

    @Test(timeout = 120_000)
    fun clipScreenShowsDetailsAndExportAction() {
        show { ClipScreen(api, clips[0].id, onEdit = {}, onBack = {}) }
        waitForText("Gancho")
        compose.onNodeWithText("Salvar na galeria", substring = true).assertExists()
        screenshot("04_corte")
    }

    @Test(timeout = 120_000)
    fun editorSendsTrimCaptionsAndWordFix() {
        var saved = false
        show { EditorScreen(api, clips[0].id, onSaved = { saved = true }, onBack = {}) }
        waitForText("Texto da legenda")
        screenshot("05_editor")
        // muda o enquadramento e corrige a primeira palavra visível
        compose.onNodeWithText("Quadro inteiro").performClick()
        compose.onNodeWithText("CAIXA ALTA").assertExists()
        compose.onNodeWithText("Salvar").performClick() // ação da barra superior
        waitFor { saved }
        val body = patches.single()
        assertTrue(body, body.contains("\"reframe\":\"fit\""))
        assertTrue(body, body.contains("\"highlight_color\":\"#FFE135\""))
        assertTrue(body, !body.contains("\"start\"")) // sem trim: não envia start/end
    }
}
