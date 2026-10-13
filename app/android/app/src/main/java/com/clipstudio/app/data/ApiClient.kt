package com.clipstudio.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.KSerializer
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.TimeUnit

class ApiException(val code: Int, message: String) : IOException(message) {
    /** Falhas transitórias que valem nova tentativa. */
    val retryable get() = code == 408 || code == 429 || code >= 500
}

/** Estado do upload informado pelo servidor: `received` bytes já gravados, ou `done` com o projeto final. */
sealed interface UploadStatus {
    data class Partial(val received: Long) : UploadStatus
    data class Done(val project: Project) : UploadStatus
}

private val JSON_TYPE = "application/json".toMediaType()
private val OCTET = "application/octet-stream".toMediaType()

/**
 * Cliente da API. Todas as chamadas são suspensas e rodam em Dispatchers.IO.
 * `baseUrl` sem barra final, ex.: http://192.168.0.10:8000
 */
class ApiClient(
    baseUrl: String,
    private val token: String,
    private val http: OkHttpClient = defaultHttp(),
) {
    val baseUrl: String = baseUrl.trim().trimEnd('/')

    companion object {
        fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(120, TimeUnit.SECONDS)
            .build()
    }

    /** URL absoluta para caminhos devolvidos pela API (ex.: /clips/x/file?kind=preview&v=2). */
    fun absolute(path: String): String = if (path.startsWith("http")) path else baseUrl + path

    fun authHeaders(): Map<String, String> = if (token.isBlank()) emptyMap() else mapOf("Authorization" to "Bearer $token")

    private fun request(path: String): Request.Builder {
        val b = Request.Builder().url(absolute(path).toHttpUrl())
        authHeaders().forEach { (k, v) -> b.header(k, v) }
        return b
    }

    private fun errorMessage(resp: Response): String {
        val body = resp.body?.string().orEmpty()
        return runCatching {
            val obj = ApiJson.parseToJsonElement(body).jsonObject
            (obj["detail"] ?: obj["error"])?.jsonPrimitive?.content
        }.getOrNull() ?: body.take(300).ifBlank { "HTTP ${resp.code}" }
    }

    private suspend fun <T> call(req: Request, ser: KSerializer<T>): T = withContext(Dispatchers.IO) {
        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw ApiException(resp.code, errorMessage(resp))
            ApiJson.decodeFromString(ser, resp.body!!.string())
        }
    }

    private suspend fun send(req: Request): Unit = withContext(Dispatchers.IO) {
        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw ApiException(resp.code, errorMessage(resp))
        }
    }

    private inline fun <reified T> jsonBody(value: T): RequestBody = ApiJson.encodeToString(value).toRequestBody(JSON_TYPE)

    suspend fun health(): Health = call(request("/health").get().build(), Health.serializer())

    suspend fun login(email: String, password: String): LoginResponse =
        call(request("/auth/login").post(jsonBody(Credentials(email, password))).build(), LoginResponse.serializer())

    suspend fun register(email: String, password: String): LoginResponse =
        call(request("/auth/register").post(jsonBody(Credentials(email, password))).build(), LoginResponse.serializer())

    suspend fun me(): Me = call(request("/me").get().build(), Me.serializer())

    suspend fun createProject(body: CreateProjectRequest): CreateProjectResponse =
        call(request("/projects").post(jsonBody(body)).build(), CreateProjectResponse.serializer())

    suspend fun listProjects(): List<Project> =
        call(request("/projects").get().build(), ListSerializer(Project.serializer()))

    suspend fun project(id: String): Project = call(request("/projects/$id").get().build(), Project.serializer())

    suspend fun startProject(id: String): Project =
        call(request("/projects/$id/start").post(ByteArray(0).toRequestBody()).build(), Project.serializer())

    suspend fun deleteProject(id: String) = send(request("/projects/$id").delete().build())

    suspend fun clips(projectId: String): List<Clip> =
        call(request("/projects/$projectId/clips").get().build(), ListSerializer(Clip.serializer()))

    suspend fun clip(id: String): Clip = call(request("/clips/$id").get().build(), Clip.serializer())

    suspend fun editClip(id: String, edit: ClipEdit): Clip =
        call(request("/clips/$id").patch(jsonBody(edit)).build(), Clip.serializer())

    suspend fun render(id: String, quality: String = "export"): Clip =
        call(request("/clips/$id/render?quality=$quality").post(ByteArray(0).toRequestBody()).build(), Clip.serializer())

    suspend fun transcript(projectId: String): Transcript =
        call(request("/projects/$projectId/transcript.json").get().build(), Transcript.serializer())

    /** Consulta quantos bytes o servidor já tem (`Content-Range: bytes * /total`). */
    suspend fun uploadStatus(uploadUrl: String, total: Long): UploadStatus = withContext(Dispatchers.IO) {
        val req = request(uploadUrl).put(ByteArray(0).toRequestBody()).header("Content-Range", "bytes */$total").build()
        http.newCall(req).execute().use { parseUpload(it) }
    }

    /** Envia `length` bytes de `data` a partir de `offset`. */
    suspend fun uploadChunk(uploadUrl: String, offset: Long, length: Long, total: Long, data: ByteArray): UploadStatus =
        withContext(Dispatchers.IO) {
            val body = data.toRequestBody(OCTET, 0, length.toInt())
            val req = request(uploadUrl).put(body)
                .header("Content-Range", "bytes $offset-${offset + length - 1}/$total").build()
            http.newCall(req).execute().use { parseUpload(it) }
        }

    private fun parseUpload(resp: Response): UploadStatus = when (resp.code) {
        308 -> UploadStatus.Partial(resp.header("Range")?.substringAfter("-")?.toLongOrNull()?.plus(1) ?: 0L)
        200, 201 -> UploadStatus.Done(ApiJson.decodeFromString(ProjectEnvelope.serializer(), resp.body!!.string()).project)
        else -> throw ApiException(resp.code, errorMessage(resp))
    }

    /**
     * Abre o download de `path` a partir de `offset` (Range). Devolve (stream, tamanho total) — o chamador fecha o stream.
     * Se o servidor ignorar o Range (200), o stream recomeça do zero e `restarted` = true.
     */
    suspend fun openDownload(path: String, offset: Long): Download = withContext(Dispatchers.IO) {
        val b = request(path).get()
        if (offset > 0) b.header("Range", "bytes=$offset-")
        val resp = http.newCall(b.build()).execute()
        if (resp.code == 416) {
            resp.close()
            return@withContext Download(InputStream.nullInputStream(), offset, restarted = false, complete = true)
        }
        if (!resp.isSuccessful) {
            val msg = errorMessage(resp)
            resp.close()
            throw ApiException(resp.code, msg)
        }
        val total = resp.header("Content-Range")?.substringAfter("/")?.toLongOrNull()
            ?: resp.body!!.contentLength().takeIf { it >= 0 } ?: -1L
        Download(resp.body!!.byteStream(), total, restarted = resp.code == 200 && offset > 0)
    }

    data class Download(val stream: InputStream, val total: Long, val restarted: Boolean, val complete: Boolean = false)
}
