package com.clipstudio.app.ui

import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.work.WorkInfo
import androidx.work.WorkManager
import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.AppGraph
import com.clipstudio.app.data.Clip
import com.clipstudio.app.data.Project
import com.clipstudio.app.work.UploadWorker
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProjectScreen(api: ApiClient, projectId: String, onClip: (String) -> Unit, onBack: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var project by remember { mutableStateOf<Project?>(null) }
    var clips by remember { mutableStateOf<List<Clip>>(emptyList()) }
    var error by remember { mutableStateOf("") }
    var confirmDelete by remember { mutableStateOf(false) }
    val uploadsFlow = remember(projectId) {
        WorkManager.getInstance(context).getWorkInfosForUniqueWorkFlow(UploadWorker.uniqueName(projectId))
    }
    val uploads by uploadsFlow.collectAsState(initial = emptyList())
    val upload = uploads.lastOrNull()

    LaunchedEffect(projectId) {
        while (isActive) {
            try {
                val p = api.project(projectId)
                project = p
                if (p.isDone) clips = api.clips(projectId)
                error = ""
                if (p.isDone || p.isFailed) delay(10_000) else delay(2_000)
            } catch (e: Exception) {
                error = e.message ?: "erro"
                delay(4_000)
            }
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text(project?.title ?: "Projeto") },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Voltar") } },
            actions = { IconButton(onClick = { confirmDelete = true }) { Icon(Icons.Default.Delete, "Apagar") } },
        )
    }) { pad ->
        LazyColumn(Modifier.padding(pad).fillMaxSize(), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            item { StatusCard(project, upload, error, onRetryUpload = {
                AppGraph.pendingUpload(projectId)?.let {
                    WorkManager.getInstance(context).cancelUniqueWork(UploadWorker.uniqueName(projectId))
                    UploadWorker.enqueue(context, projectId, it.uploadUrl, Uri.parse(it.uri), it.size, it.title)
                }
            }, onReprocess = {
                scope.launch { runCatching { project = api.startProject(projectId) }.onFailure { error = it.message ?: "" } }
            }) }
            items(clips, key = { it.id }) { c -> ClipRow(api, c) { onClip(c.id) } }
        }
    }

    if (confirmDelete) {
        AlertDialog(onDismissRequest = { confirmDelete = false },
            title = { Text("Apagar projeto?") },
            text = { Text("O vídeo enviado e os cortes no servidor serão apagados. Vídeos já salvos na galeria continuam.") },
            confirmButton = { TextButton(onClick = {
                confirmDelete = false
                scope.launch {
                    runCatching { api.deleteProject(projectId) }
                        .onSuccess {
                            WorkManager.getInstance(context).cancelUniqueWork(UploadWorker.uniqueName(projectId))
                            AppGraph.forgetUpload(projectId)
                            onBack()
                        }
                        .onFailure { error = it.message ?: "" }
                }
            }) { Text("Apagar") } },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancelar") } })
    }
}

@Composable
private fun StatusCard(p: Project?, upload: WorkInfo?, error: String, onRetryUpload: () -> Unit, onReprocess: () -> Unit) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            val uploading = upload != null && !upload.state.isFinished && p?.status in setOf(null, "CREATED", "UPLOADING")
            when {
                uploading -> {
                    val sent = upload!!.progress.getLong(UploadWorker.KEY_SENT, 0)
                    val total = upload.progress.getLong(UploadWorker.KEY_TOTAL, 0)
                    Text(if (upload.state == WorkInfo.State.ENQUEUED) "Aguardando rede para enviar…" else "Enviando vídeo",
                        style = MaterialTheme.typography.titleMedium)
                    if (total > 0) {
                        LinearProgressIndicator(progress = { sent.toFloat() / total }, modifier = Modifier.fillMaxWidth())
                        Text("%.1f de %.1f MB".format(sent / 1e6, total / 1e6), style = MaterialTheme.typography.bodySmall)
                    } else LinearProgressIndicator(Modifier.fillMaxWidth())
                }
                upload?.state == WorkInfo.State.FAILED && p?.status in setOf("CREATED", "UPLOADING") -> {
                    Text("O envio falhou", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.error)
                    Text(upload!!.outputData.getString(UploadWorker.KEY_ERROR).orEmpty())
                    Button(onClick = onRetryUpload) { Text("Tentar de novo") }
                }
                p == null -> LinearProgressIndicator(Modifier.fillMaxWidth())
                p.isDone -> {
                    Text("Pronto", style = MaterialTheme.typography.titleMedium)
                    Text("Vídeo de ${formatSeconds(p.duration)} · curadoria ${when { p.curation == "llm+heuristic" -> "por IA + local"; p.curation.endsWith("+visual") -> "por IA (texto + imagem)"; p.curation.startsWith("llm") -> "por IA"; else -> "local" }} · idioma ${p.language}",
                        style = MaterialTheme.typography.bodySmall)
                    p.warnings.forEach { Text("⚠ $it", style = MaterialTheme.typography.bodySmall) }
                }
                p.isFailed -> {
                    Text("Falhou", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.error)
                    Text(p.error)
                    if (p.error.startsWith("arquivo inválido").not()) Button(onClick = onReprocess) { Text("Processar de novo") }
                }
                else -> {
                    Text(stageLabel(p.stage), style = MaterialTheme.typography.titleMedium)
                    if (p.status == "QUEUED") Text("Posição na fila: ${(p.queuePosition ?: 0) + 1}", style = MaterialTheme.typography.bodySmall)
                    LinearProgressIndicator(progress = { p.progress.toFloat() }, modifier = Modifier.fillMaxWidth())
                    if (p.message.isNotBlank()) Text(p.message, style = MaterialTheme.typography.bodySmall)
                }
            }
            if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun ClipRow(api: ApiClient, c: Clip, onClick: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Row(Modifier.padding(10.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            RemoteImage(api, c.thumbnailUrl, Modifier.width(72.dp).aspectRatio(9f / 16f).clip(RoundedCornerShape(8.dp)))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("#${c.rank} · ${c.title}", style = MaterialTheme.typography.titleSmall, maxLines = 2)
                Text("${formatSeconds(c.start)} – ${formatSeconds(c.end)} · ${"%.0f".format(c.duration)} s · nota ${"%.0f".format(c.score)}",
                    style = MaterialTheme.typography.bodySmall)
                val state = when {
                    c.renderPending -> "Renderizando…"
                    c.exportReady -> "Export 1080p pronto"
                    !c.qa.ok && c.qa.problems.isNotEmpty() -> "⚠ ${c.qa.problems.first()}"
                    else -> REFRAME_OPTIONS.firstOrNull { it.first == c.reframe }?.second ?: c.reframe
                }
                Text(state, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
            }
        }
    }
}
