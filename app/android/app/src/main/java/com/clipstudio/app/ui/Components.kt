package com.clipstudio.app.ui

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.annotation.OptIn
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import com.clipstudio.app.data.ApiClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Request

private val thumbCache = LruCache<String, Bitmap>(40)

/** Imagem da API (com o header de autenticação), com cache em memória. */
@Composable
fun RemoteImage(api: ApiClient, path: String?, modifier: Modifier = Modifier) {
    val bitmap by produceState<Bitmap?>(initialValue = path?.let { thumbCache.get(it) }, path) {
        if (path == null || value != null) return@produceState
        value = withContext(Dispatchers.IO) {
            runCatching {
                val req = Request.Builder().url(api.absolute(path)).apply {
                    api.authHeaders().forEach { (k, v) -> header(k, v) }
                }.build()
                ApiClient.defaultHttp().newCall(req).execute().use { r ->
                    if (!r.isSuccessful) null else BitmapFactory.decodeStream(r.body!!.byteStream())
                }
            }.getOrNull()
        }?.also { thumbCache.put(path, it) }
    }
    Box(modifier.background(Color(0xFF1E1E1E))) {
        bitmap?.let { Image(it.asImageBitmap(), contentDescription = null, contentScale = ContentScale.Crop, modifier = Modifier.matchParentSize()) }
    }
}

/** Player Media3 para uma URL autenticada da API. Recria o player quando a URL (versão) muda. */
@OptIn(UnstableApi::class)
@Composable
fun VideoPlayer(api: ApiClient, path: String, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val player = remember(path) {
        val http = DefaultHttpDataSource.Factory().setDefaultRequestProperties(api.authHeaders())
        ExoPlayer.Builder(context).setMediaSourceFactory(DefaultMediaSourceFactory(http)).build().apply {
            setMediaItem(MediaItem.fromUri(api.absolute(path)))
            prepare()
            playWhenReady = true
        }
    }
    DisposableEffect(player) { onDispose { player.release() } }
    AndroidView(
        factory = { PlayerView(it).apply { useController = true } },
        update = { it.player = player },
        modifier = modifier.background(Color.Black),
    )
}

fun formatSeconds(s: Double): String {
    val total = s.toInt()
    val tenth = ((s - total) * 10).toInt()
    return "%d:%02d.%d".format(total / 60, total % 60, tenth)
}

fun stageLabel(stage: String): String = when (stage) {
    "QUEUED" -> "Na fila"
    "IMPORT" -> "Preparando"
    "TRANSCRIBE" -> "Transcrevendo"
    "ANALYZE" -> "Analisando cenas e rostos"
    "CURATE" -> "Escolhendo os melhores trechos"
    "REFINE" -> "Ajustando limites"
    "REFRAME" -> "Enquadrando"
    "RENDER" -> "Gerando os cortes"
    "QA" -> "Verificando qualidade"
    "COMPLETE" -> "Pronto"
    "FAILED" -> "Falhou"
    else -> stage
}
