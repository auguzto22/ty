package com.clipstudio.app.data

import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import java.io.IOException
import kotlin.coroutines.coroutineContext
import kotlin.random.Random

/** Leitura posicional do arquivo a enviar (no Android: FileChannel de um ParcelFileDescriptor). */
interface UploadSource {
    val size: Long
    /** Lê até `length` bytes da posição `offset` do arquivo para `buffer[at..]`; devolve quantos leu (-1 no fim). */
    fun read(offset: Long, buffer: ByteArray, at: Int, length: Int): Int
}

class ByteArraySource(private val bytes: ByteArray) : UploadSource {
    override val size: Long get() = bytes.size.toLong()
    override fun read(offset: Long, buffer: ByteArray, at: Int, length: Int): Int {
        if (offset >= bytes.size) return -1
        val n = minOf(length.toLong(), bytes.size - offset).toInt()
        System.arraycopy(bytes, offset.toInt(), buffer, at, n)
        return n
    }
}

/**
 * Upload retomável no protocolo do backend: consulta o offset (`bytes * /total`), envia partes com
 * `Content-Range` e segue o `Range` das respostas 308. Falhas de rede/5xx/429 são repetidas com
 * espera exponencial + jitter; após `maxRetries` falhas seguidas, a exceção sobe (o WorkManager reagenda).
 */
class ResumableUploader(
    private val api: ApiClient,
    private val chunkSize: Int = 8 * 1024 * 1024,
    private val maxRetries: Int = 5,
    private val baseDelayMs: Long = 1_000,
    private val maxDelayMs: Long = 30_000,
) {
    suspend fun upload(uploadUrl: String, source: UploadSource, onProgress: (Long, Long) -> Unit = { _, _ -> }): Project {
        val total = source.size
        val buffer = ByteArray(chunkSize)
        var failures = 0
        var status: UploadStatus = retrying(failures) { api.uploadStatus(uploadUrl, total) }
        while (true) {
            coroutineContext.ensureActive()
            val offset = when (val s = status) {
                is UploadStatus.Done -> {
                    onProgress(total, total)
                    return s.project
                }
                is UploadStatus.Partial -> s.received
            }
            onProgress(offset, total)
            val want = minOf(chunkSize.toLong(), total - offset).toInt()
            var read = 0
            while (read < want) {
                val n = source.read(offset + read, buffer, read, want - read)
                if (n <= 0) break
                read += n
            }
            if (read <= 0) throw IOException("arquivo terminou antes do esperado ($offset/$total)")
            status = try {
                api.uploadChunk(uploadUrl, offset, read.toLong(), total, buffer).also { failures = 0 }
            } catch (e: IOException) {
                if (e is ApiException && !e.retryable) throw e
                failures++
                if (failures > maxRetries) throw e
                backoff(failures)
                retrying(failures) { api.uploadStatus(uploadUrl, total) } // descobre o que realmente chegou
            }
        }
    }

    private suspend fun <T> retrying(start: Int, block: suspend () -> T): T {
        var attempt = start
        while (true) {
            try {
                return block()
            } catch (e: IOException) {
                if (e is ApiException && !e.retryable) throw e
                attempt++
                if (attempt > maxRetries) throw e
                backoff(attempt)
            }
        }
    }

    private suspend fun backoff(attempt: Int) {
        val exp = (baseDelayMs shl (attempt - 1).coerceAtMost(10)).coerceAtMost(maxDelayMs)
        delay(exp / 2 + Random.nextLong(exp / 2 + 1))
    }
}
