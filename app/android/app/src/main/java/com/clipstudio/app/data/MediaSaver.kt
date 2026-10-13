package com.clipstudio.app.data

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import java.io.File
import java.io.IOException

/** Copia um MP4 para a galeria em Movies/ClipStudio (MediaStore, sem permissão de armazenamento no Android 10+). */
object MediaSaver {
    const val FOLDER = "ClipStudio"

    fun saveVideo(context: Context, file: File, displayName: String): Uri {
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, displayName)
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH, "${Environment.DIRECTORY_MOVIES}/$FOLDER")
            put(MediaStore.Video.Media.IS_PENDING, 1)
        }
        val collection = MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        val uri = resolver.insert(collection, values) ?: throw IOException("MediaStore recusou o arquivo")
        try {
            resolver.openOutputStream(uri)?.use { out -> file.inputStream().use { it.copyTo(out) } }
                ?: throw IOException("não foi possível abrir a saída")
            resolver.update(uri, ContentValues().apply { put(MediaStore.Video.Media.IS_PENDING, 0) }, null, null)
            return uri
        } catch (e: Exception) {
            resolver.delete(uri, null, null)
            throw e
        }
    }

    fun safeName(title: String, rank: Int, version: Int): String {
        val base = title.ifBlank { "corte" }.replace(Regex("[^\\p{L}\\p{N} _-]"), "").trim().take(40).ifBlank { "corte" }
        return "${rank.toString().padStart(2, '0')}_${base}_v$version.mp4"
    }
}
