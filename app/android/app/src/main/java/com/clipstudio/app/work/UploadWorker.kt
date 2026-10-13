package com.clipstudio.app.work

import android.content.Context
import android.net.Uri
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.clipstudio.app.data.ApiException
import com.clipstudio.app.data.AppGraph
import com.clipstudio.app.data.ResumableUploader
import com.clipstudio.app.data.UploadSource
import java.io.FileInputStream
import java.io.IOException
import java.nio.ByteBuffer
import java.util.concurrent.TimeUnit

/** Envia o vídeo (retomável) e inicia o processamento. Sobrevive a troca de rede e ao app fechado. */
class UploadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    companion object {
        const val KEY_PROJECT = "project_id"
        const val KEY_URL = "upload_url"
        const val KEY_URI = "uri"
        const val KEY_SIZE = "size"
        const val KEY_TITLE = "title"
        const val KEY_SENT = "sent"
        const val KEY_TOTAL = "total"
        const val KEY_ERROR = "error"

        fun uniqueName(projectId: String) = "upload-$projectId"

        fun enqueue(context: Context, projectId: String, uploadUrl: String, uri: Uri, size: Long, title: String) {
            val req = OneTimeWorkRequestBuilder<UploadWorker>()
                .setInputData(workDataOf(KEY_PROJECT to projectId, KEY_URL to uploadUrl, KEY_URI to uri.toString(),
                    KEY_SIZE to size, KEY_TITLE to title))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .addTag("upload")
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(uniqueName(projectId), ExistingWorkPolicy.KEEP, req)
        }
    }

    private val notificationId get() = 1000 + (inputData.getString(KEY_PROJECT).hashCode() and 0xffff)

    override suspend fun getForegroundInfo() =
        Notifications.foreground(applicationContext, notificationId, "Enviando vídeo", inputData.getString(KEY_TITLE).orEmpty(), 0, 0)

    override suspend fun doWork(): Result {
        val projectId = inputData.getString(KEY_PROJECT) ?: return Result.failure()
        val url = inputData.getString(KEY_URL) ?: return Result.failure()
        val uri = Uri.parse(inputData.getString(KEY_URI) ?: return Result.failure())
        val title = inputData.getString(KEY_TITLE).orEmpty()
        val api = try { AppGraph.requireApi(applicationContext) } catch (e: IllegalStateException) {
            return Result.failure(workDataOf(KEY_ERROR to e.message))
        }
        runCatching { setForeground(getForegroundInfo()) } // pode ser negado em segundo plano; segue sem

        val pfd = try {
            applicationContext.contentResolver.openFileDescriptor(uri, "r")
        } catch (e: Exception) {
            null
        } ?: return Result.failure(workDataOf(KEY_ERROR to "Sem acesso ao vídeo selecionado"))

        return pfd.use {
            FileInputStream(it.fileDescriptor).channel.use { channel ->
                val source = object : UploadSource {
                    override val size: Long = channel.size()
                    override fun read(offset: Long, buffer: ByteArray, at: Int, length: Int): Int =
                        channel.read(ByteBuffer.wrap(buffer, at, length), offset)
                }
                var lastNotify = 0L
                try {
                    ResumableUploader(api).upload(url, source) { sent, total ->
                        setProgressAsync(workDataOf(KEY_SENT to sent, KEY_TOTAL to total))
                        val now = System.currentTimeMillis()
                        if (now - lastNotify > 1000) {
                            lastNotify = now
                            runCatching {
                                setForegroundAsync(Notifications.foreground(applicationContext, notificationId,
                                    "Enviando vídeo", title, sent, total))
                            }
                        }
                    }
                    try {
                        api.startProject(projectId)
                    } catch (e: ApiException) {
                        if (e.code != 409) throw e // 409 = já iniciado
                    }
                    Result.success()
                } catch (e: ApiException) {
                    if (e.retryable) Result.retry() else Result.failure(workDataOf(KEY_ERROR to e.message))
                } catch (e: IOException) {
                    Result.retry()
                }
            }
        }
    }
}
