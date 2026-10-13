package com.clipstudio.app

import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.ByteArraySource
import com.clipstudio.app.data.CreateProjectRequest
import com.clipstudio.app.data.ProjectPrefs
import com.clipstudio.app.data.ResumableUploader
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File

/**
 * Integração com o backend REAL (não roda por padrão). Exige o servidor e o worker no ar:
 *   CLIPSTUDIO_URL=http://127.0.0.1:8000 CLIPSTUDIO_TOKEN=... CLIPSTUDIO_VIDEO=/caminho/video.mp4 ./gradlew testDebugUnitTest
 */
class LiveServerTest {
    @Test
    fun uploadProcessAndDownloadAgainstRealBackend() = runBlocking {
        val url = System.getenv("CLIPSTUDIO_URL")
        val video = System.getenv("CLIPSTUDIO_VIDEO")?.let(::File)
        assumeTrue("defina CLIPSTUDIO_URL e CLIPSTUDIO_VIDEO", url != null && video?.exists() == true)
        val api = ApiClient(url!!, System.getenv("CLIPSTUDIO_TOKEN").orEmpty())
        val bytes = video!!.readBytes()
        val created = api.createProject(CreateProjectRequest("teste-jvm", video.name, bytes.size.toLong(), "video/mp4",
            ProjectPrefs(minDuration = 10.0, maxDuration = 25.0, maxClips = 2, language = "pt")))
        val uploaded = ResumableUploader(api, chunkSize = 1_000_000).upload(created.upload.url, ByteArraySource(bytes))
        assertEquals("UPLOADED", uploaded.status)
        api.startProject(created.project.id)
        var p = api.project(created.project.id)
        val deadline = System.currentTimeMillis() + 20 * 60_000
        while (!p.isDone && !p.isFailed && System.currentTimeMillis() < deadline) {
            delay(3_000)
            p = api.project(created.project.id)
        }
        assertTrue("projeto: ${p.status} ${p.error}", p.isDone)
        val clips = api.clips(p.id)
        assertTrue(clips.isNotEmpty())
        val dl = api.openDownload(clips.first().preview!!.url, 100)
        val rest = dl.stream.use { it.readBytes() }
        assertEquals(dl.total, 100L + rest.size) // Range respeitado: veio tudo menos os 100 primeiros bytes
        api.deleteProject(p.id)
    }
}
