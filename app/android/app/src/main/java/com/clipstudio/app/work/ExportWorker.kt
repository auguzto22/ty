package com.clipstudio.app.work

import android.content.Context
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
import com.clipstudio.app.data.MediaSaver
import kotlinx.coroutines.delay
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Pede o export 1080x1920, espera o render, baixa com Range (retomável) e salva em Movies/ClipStudio. */
class ExportWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    companion object {
        const val KEY_CLIP = "clip_id"
        const val KEY_STAGE = "stage" // "render" | "download" | "save"
        const val KEY_DONE = "done"
        const val KEY_TOTAL = "total"
        const val KEY_URI = "saved_uri"
        const val KEY_ERROR = "error"
        private const val RENDER_TIMEOUT_MS = 45 * 60 * 1000L

        fun uniqueName(clipId: String, version: Int) = "export-$clipId-v$version"

        fun enqueue(context: Context, clipId: String, version: Int) {
            val req = OneTimeWorkRequestBuilder<ExportWorker>()
                .setInputData(workDataOf(KEY_CLIP to clipId))
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .addTag("export").addTag("clip-$clipId")
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(uniqueName(clipId, version), ExistingWorkPolicy.KEEP, req)
        }
    }

    private val notificationId get() = 2000 + (inputData.getString(KEY_CLIP).hashCode() and 0xffff)

    override suspend fun getForegroundInfo() =
        Notifications.foreground(applicationContext, notificationId, "Exportando corte", "Renderizando no servidor", 0, 0)

    override suspend fun doWork(): Result {
        val clipId = inputData.getString(KEY_CLIP) ?: return Result.failure()
        val api = try { AppGraph.requireApi(applicationContext) } catch (e: IllegalStateException) {
            return Result.failure(workDataOf(KEY_ERROR to e.message))
        }
        runCatching { setForeground(getForegroundInfo()) }
        try {
            setProgress(workDataOf(KEY_STAGE to "render"))
            var clip = api.clip(clipId)
            if (!clip.exportReady) clip = api.render(clipId, "export")
            val deadline = System.currentTimeMillis() + RENDER_TIMEOUT_MS
            while (!clip.exportReady) {
                if (System.currentTimeMillis() > deadline) return Result.retry()
                delay(3_000)
                clip = api.clip(clipId)
            }
            val export = clip.export!!

            val dir = File(applicationContext.cacheDir, "exports").apply { mkdirs() }
            val part = File(dir, "${clipId}_v${export.version}.mp4.partial")
            var have = part.length()
            val dl = api.openDownload(export.url, have)
            if (dl.restarted) have = 0
            dl.stream.use { input ->
                FileOutputStream(part, have > 0).use { out ->
                    val buf = ByteArray(64 * 1024)
                    var done = have
                    var lastReport = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        val now = System.currentTimeMillis()
                        if (now - lastReport > 500) {
                            lastReport = now
                            setProgress(workDataOf(KEY_STAGE to "download", KEY_DONE to done, KEY_TOTAL to dl.total))
                            runCatching {
                                setForeground(Notifications.foreground(applicationContext, notificationId,
                                    "Exportando corte", "Baixando", done, dl.total))
                            }
                        }
                    }
                }
            }
            if (dl.total > 0 && part.length() != dl.total) return Result.retry() // conexão caiu: retoma do parcial

            setProgress(workDataOf(KEY_STAGE to "save"))
            val uri = MediaSaver.saveVideo(applicationContext, part,
                MediaSaver.safeName(clip.title, clip.rank, export.version))
            part.delete()
            return Result.success(workDataOf(KEY_URI to uri.toString()))
        } catch (e: ApiException) {
            return if (e.retryable) Result.retry() else Result.failure(workDataOf(KEY_ERROR to e.message))
        } catch (e: IOException) {
            return Result.retry()
        }
    }
}
