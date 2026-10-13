package com.clipstudio.app.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.Clip
import com.clipstudio.app.work.ExportWorker
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ClipScreen(api: ApiClient, clipId: String, onEdit: () -> Unit, onBack: () -> Unit) {
    val context = LocalContext.current
    var clip by remember { mutableStateOf<Clip?>(null) }
    var error by remember { mutableStateOf("") }

    LaunchedEffect(clipId) {
        while (isActive) {
            try {
                val c = api.clip(clipId)
                clip = c
                error = ""
                delay(if (c.renderPending || c.preview?.stale == true) 2_000 else 8_000)
            } catch (e: Exception) {
                error = e.message ?: "erro"
                delay(4_000)
            }
        }
    }

    val c = clip
    val exportFlow = remember(clipId, c?.version) {
        c?.let { WorkManager.getInstance(context).getWorkInfosForUniqueWorkFlow(ExportWorker.uniqueName(it.id, it.version)) }
    }
    val exports by (exportFlow ?: kotlinx.coroutines.flow.flowOf(emptyList())).collectAsState(initial = emptyList())
    val export = exports.lastOrNull()

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(c?.let { "Corte #${it.rank}" } ?: "Corte") },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Voltar") } },
            actions = { if (c != null) IconButton(onClick = onEdit) { Icon(Icons.Default.Edit, "Editar") } },
        )
    }) { pad ->
        Column(Modifier.padding(pad).padding(12.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (c == null) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
                if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error)
                return@Column
            }
            val preview = c.preview
            if (preview != null && !preview.stale) {
                VideoPlayer(api, preview.url, Modifier.fillMaxWidth().heightIn(max = 520.dp).aspectRatio(9f / 16f)
                    .align(Alignment.CenterHorizontally))
            } else {
                Text("Gerando nova prévia…", style = MaterialTheme.typography.bodyMedium)
                LinearProgressIndicator(Modifier.fillMaxWidth())
            }
            Text(c.title, style = MaterialTheme.typography.titleMedium)
            Text("${formatSeconds(c.start)} – ${formatSeconds(c.end)} (${"%.1f".format(c.duration)} s) · versão ${c.version}",
                style = MaterialTheme.typography.bodySmall)
            if (c.hook.isNotBlank()) Text("Gancho: “${c.hook}”", style = MaterialTheme.typography.bodyMedium)
            Text("Nota ${"%.0f".format(c.score)} · gancho ${"%.0f".format(c.scores.hook)} · coerência ${"%.0f".format(c.scores.coherence)} · " +
                "áudio ${"%.0f".format(c.scores.audio)} · visual ${"%.0f".format(c.scores.visual)}", style = MaterialTheme.typography.bodySmall)
            if (c.qa.problems.isNotEmpty()) Text("⚠ QA: ${c.qa.problems.joinToString("; ")}", color = MaterialTheme.colorScheme.error)

            ExportSection(c, export, onExport = { ExportWorker.enqueue(context, c.id, c.version) }, onShare = { uri ->
                val send = Intent(Intent.ACTION_SEND).setType("video/mp4").putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                context.startActivity(Intent.createChooser(send, "Compartilhar corte"))
            }, onOpen = { uri ->
                context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "video/mp4")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
            })
            if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun ExportSection(c: Clip, work: WorkInfo?, onExport: () -> Unit, onShare: (Uri) -> Unit, onOpen: (Uri) -> Unit) {
    when (work?.state) {
        null, WorkInfo.State.CANCELLED -> Button(onClick = onExport, enabled = !c.renderPending, modifier = Modifier.fillMaxWidth()) {
            Text(if (c.exportReady) "Salvar na galeria (1080p)" else "Exportar 1080p e salvar na galeria")
        }
        WorkInfo.State.SUCCEEDED -> {
            val uri = Uri.parse(work.outputData.getString(ExportWorker.KEY_URI))
            Text("Salvo em Filmes/ClipStudio", style = MaterialTheme.typography.titleSmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { onShare(uri) }) {
                    Icon(Icons.Default.Share, null)
                    Text(" Compartilhar")
                }
                OutlinedButton(onClick = { onOpen(uri) }) { Text("Abrir") }
            }
        }
        WorkInfo.State.FAILED -> {
            Text("Falha no export: ${work.outputData.getString(ExportWorker.KEY_ERROR).orEmpty()}", color = MaterialTheme.colorScheme.error)
            Button(onClick = onExport) { Text("Tentar de novo") }
        }
        else -> {
            val stage = work.progress.getString(ExportWorker.KEY_STAGE)
            val done = work.progress.getLong(ExportWorker.KEY_DONE, 0)
            val total = work.progress.getLong(ExportWorker.KEY_TOTAL, 0)
            Text(when (stage) {
                "download" -> "Baixando %.1f de %.1f MB".format(done / 1e6, total / 1e6)
                "save" -> "Salvando na galeria…"
                else -> if (work.state == WorkInfo.State.ENQUEUED) "Aguardando rede…" else "Renderizando 1080p no servidor…"
            })
            if (stage == "download" && total > 0) LinearProgressIndicator(progress = { done.toFloat() / total }, modifier = Modifier.fillMaxWidth())
            else LinearProgressIndicator(Modifier.fillMaxWidth())
        }
    }
}
