package com.clipstudio.app.ui

import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.AppGraph
import com.clipstudio.app.data.CreateProjectRequest
import com.clipstudio.app.data.Project
import com.clipstudio.app.data.ProjectPrefs
import com.clipstudio.app.data.RenderPrefs
import com.clipstudio.app.work.UploadWorker
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class PickedVideo(val uri: Uri, val name: String, val size: Long)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProjectsScreen(api: ApiClient, onOpen: (String) -> Unit, onSettings: () -> Unit) {
    val context = LocalContext.current
    var projects by remember { mutableStateOf<List<Project>?>(null) }
    var error by remember { mutableStateOf("") }
    var picked by remember { mutableStateOf<PickedVideo?>(null) }

    LaunchedEffect(api) {
        while (isActive) {
            try {
                projects = api.listProjects()
                error = ""
            } catch (e: Exception) {
                error = if (e is com.clipstudio.app.data.ApiException && e.code == 401)
                    "Sessão expirada ou token inválido. Toque na engrenagem e entre de novo."
                else "Sem conexão com o servidor: ${e.message}"
            }
            delay(if (projects.orEmpty().any { it.isBusy }) 2_000 else 6_000)
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        runCatching { context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
        var name = "video.mp4"
        var size = -1L
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                name = c.getString(0) ?: name
                size = if (c.isNull(1)) -1 else c.getLong(1)
            }
        }
        if (size <= 0) {
            size = context.contentResolver.openFileDescriptor(uri, "r")?.use { it.statSize } ?: -1
        }
        if (size <= 0) error = "Não foi possível ler o tamanho do vídeo" else picked = PickedVideo(uri, name, size)
    }

    Scaffold(
        topBar = {
            TopAppBar(title = { Text("ClipStudio") }, actions = {
                IconButton(onClick = onSettings) { Icon(Icons.Default.Settings, contentDescription = "Configurações") }
            })
        },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = { picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.VideoOnly)) },
                icon = { Icon(Icons.Default.Add, contentDescription = null) },
                text = { Text("Novo vídeo") },
            )
        },
    ) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(16.dp))
            val list = projects
            when {
                list == null -> LinearProgressIndicator(Modifier.fillMaxWidth().padding(16.dp))
                list.isEmpty() -> Box(Modifier.fillMaxSize().padding(24.dp)) {
                    Text("Nenhum projeto ainda. Toque em \"Novo vídeo\" para gerar cortes.")
                }
                else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(list, key = { it.id }) { p -> ProjectCard(p) { onOpen(p.id) } }
                    item { Spacer(Modifier.height(80.dp)) }
                }
            }
        }
    }

    picked?.let { video ->
        NewProjectDialog(api, video, onDismiss = { picked = null }, onCreated = { id -> picked = null; onOpen(id) })
    }
}

@Composable
private fun ProjectCard(p: Project, onClick: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(p.title.ifBlank { "Sem título" }, style = MaterialTheme.typography.titleMedium)
            val detail = when {
                p.isDone -> "Pronto · ${formatSeconds(p.duration)}"
                p.isFailed -> "Falhou: ${p.error.take(80)}"
                p.status == "CREATED" || p.status == "UPLOADING" -> "Enviando…"
                else -> "${stageLabel(p.stage)} · ${(p.progress * 100).toInt()}%"
            }
            Text(detail, style = MaterialTheme.typography.bodyMedium,
                color = if (p.isFailed) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
            if (p.status == "PROCESSING") LinearProgressIndicator(progress = { p.progress.toFloat() }, modifier = Modifier.fillMaxWidth())
        }
    }
}

@Composable
private fun NewProjectDialog(api: ApiClient, video: PickedVideo, onDismiss: () -> Unit, onCreated: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var title by remember { mutableStateOf(video.name.substringBeforeLast('.')) }
    var minD by remember { mutableStateOf("20") }
    var maxD by remember { mutableStateOf("60") }
    var clips by remember { mutableStateOf("3") }
    var language by remember { mutableStateOf("pt") }
    var reframe by remember { mutableStateOf("auto") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text("Novo projeto") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("${video.name} · ${"%.1f".format(video.size / 1e6)} MB", style = MaterialTheme.typography.bodySmall)
                OutlinedTextField(title, { title = it }, label = { Text("Título") }, singleLine = true)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    NumberField("Mín. (s)", minD, Modifier.weight(1f)) { minD = it }
                    NumberField("Máx. (s)", maxD, Modifier.weight(1f)) { maxD = it }
                    NumberField("Cortes", clips, Modifier.weight(1f)) { clips = it }
                }
                OutlinedTextField(language, { language = it.take(5) }, label = { Text("Idioma (pt, en… vazio = auto)") }, singleLine = true)
                Text("Enquadramento", style = MaterialTheme.typography.labelLarge)
                ReframeChips(reframe) { reframe = it }
                Text("Os cortes são escolhidos pela IA do servidor (transcrição e quadros do vídeo).",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error)
                if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            }
        },
        confirmButton = {
            TextButton(enabled = !busy, onClick = {
                val min = minD.toDoubleOrNull()
                val max = maxD.toDoubleOrNull()
                val n = clips.toIntOrNull()
                if (min == null || max == null || n == null || min > max || n !in 1..20) {
                    error = "Confira duração mínima/máxima e quantidade (1–20)"
                    return@TextButton
                }
                busy = true
                scope.launch {
                    try {
                        val prefs = ProjectPrefs(minDuration = min, maxDuration = max, maxClips = n, language = language.trim(),
                            render = RenderPrefs(reframe = reframe))
                        val type = context.contentResolver.getType(video.uri) ?: "video/mp4"
                        val created = api.createProject(CreateProjectRequest(title, video.name, video.size, type, prefs))
                        AppGraph.rememberUpload(created.project.id, created.upload.url, video.uri.toString(), video.size, title)
                        UploadWorker.enqueue(context, created.project.id, created.upload.url, video.uri, video.size, title)
                        onCreated(created.project.id)
                    } catch (e: Exception) {
                        error = e.message ?: "Erro ao criar projeto"
                        busy = false
                    }
                }
            }) { Text("Enviar") }
        },
        dismissButton = { TextButton(enabled = !busy, onClick = onDismiss) { Text("Cancelar") } },
    )
}

@Composable
private fun NumberField(label: String, value: String, modifier: Modifier, onChange: (String) -> Unit) {
    OutlinedTextField(value, { v -> onChange(v.filter { it.isDigit() || it == '.' }.take(5)) }, label = { Text(label) },
        singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = modifier)
}

val REFRAME_OPTIONS = listOf("auto" to "Automático", "face" to "Rosto", "fit" to "Quadro inteiro", "center" to "Centro")

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ReframeChips(selected: String, onSelect: (String) -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        REFRAME_OPTIONS.forEach { (value, label) ->
            FilterChip(selected = selected == value, onClick = { onSelect(value) }, label = { Text(label) })
        }
    }
}
