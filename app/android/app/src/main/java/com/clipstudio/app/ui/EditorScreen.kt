package com.clipstudio.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RangeSlider
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.Clip
import com.clipstudio.app.data.ClipEdit
import com.clipstudio.app.data.Transcript
import kotlinx.coroutines.launch

/** Cores de destaque da palavra ativa (hex aceito pelo backend). */
val HIGHLIGHTS = listOf("#FFE135", "#39FF14", "#00E5FF", "#FF4FA3", "#FFFFFF")

const val TRIM_MARGIN = 30.0 // s além do corte original que o usuário pode alcançar

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun EditorScreen(api: ApiClient, clipId: String, onSaved: () -> Unit, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var clip by remember { mutableStateOf<Clip?>(null) }
    var transcript by remember { mutableStateOf<Transcript?>(null) }
    var duration by remember { mutableStateOf(0.0) }
    var error by remember { mutableStateOf("") }
    var saving by remember { mutableStateOf(false) }

    var range by remember { mutableStateOf(0f..1f) }
    var reframe by remember { mutableStateOf("auto") }
    var captionsOn by remember { mutableStateOf(true) }
    var uppercase by remember { mutableStateOf(true) }
    var highlight by remember { mutableStateOf(HIGHLIGHTS[0]) }
    var position by remember { mutableStateOf(0.72f) }
    val edits = remember { mutableStateMapOf<Int, String>() }
    var editing by remember { mutableStateOf<Int?>(null) }

    fun save() {
        val c = clip ?: return
        saving = true
        scope.launch {
            try {
                val trimChanged = kotlin.math.abs(range.start - c.start) > 0.05 || kotlin.math.abs(range.endInclusive - c.end) > 0.05
                api.editClip(c.id, ClipEdit(
                    start = if (trimChanged) range.start.toDouble() else null,
                    end = if (trimChanged) range.endInclusive.toDouble() else null,
                    reframe = reframe.takeIf { it != c.reframe },
                    captions = c.captions.copy(enabled = captionsOn, uppercase = uppercase,
                        highlightColor = highlight, position = position.toDouble()),
                    wordOverrides = edits.toMap(),
                ))
                onSaved()
            } catch (e: Exception) {
                error = e.message ?: "erro ao salvar"
                saving = false
            }
        }
    }

    LaunchedEffect(clipId) {
        try {
            val c = api.clip(clipId)
            val p = api.project(c.projectId)
            transcript = api.transcript(c.projectId)
            duration = p.duration
            range = c.start.toFloat()..c.end.toFloat()
            reframe = c.reframe.let { if (it == "auto") "auto" else it }
            captionsOn = c.captions.enabled
            uppercase = c.captions.uppercase
            highlight = c.captions.highlightColor
            position = c.captions.position.toFloat()
            c.wordOverrides.forEach { (k, v) -> k.toIntOrNull()?.let { edits[it] = v } }
            clip = c
        } catch (e: Exception) {
            error = e.message ?: "erro ao carregar"
        }
    }

    Scaffold(topBar = {
        TopAppBar(
            title = { Text("Editar corte") },
            navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Voltar") } },
            actions = { TextButton(onClick = { save() }, enabled = clip != null && !saving) { Text("Salvar") } },
        )
    }) { pad ->
        val c = clip
        if (c == null) {
            Column(Modifier.padding(pad).padding(16.dp)) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
                if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error)
            }
            return@Scaffold
        }
        val lo = maxOf(0.0, c.start - TRIM_MARGIN).toFloat()
        val hi = (if (duration > 0) minOf(duration, c.end + TRIM_MARGIN) else c.end + TRIM_MARGIN).toFloat()
        val words = transcript?.words.orEmpty().withIndex()
            .filter { (_, w) -> w.end > range.start && w.start < range.endInclusive }

        LazyColumn(Modifier.padding(pad).fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            item {
                Text("Trecho", style = MaterialTheme.typography.titleMedium)
                RangeSlider(value = range, onValueChange = { range = it }, valueRange = lo..hi)
                Text("${formatSeconds(range.start.toDouble())} – ${formatSeconds(range.endInclusive.toDouble())} · " +
                    "%.1f s".format(range.endInclusive - range.start), style = MaterialTheme.typography.bodyMedium)
                Text("O corte começa e termina exatamente onde você soltar; ajuste perto das pausas para não cortar palavras.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            item {
                Text("Enquadramento", style = MaterialTheme.typography.titleMedium)
                ReframeChips(reframe) { reframe = it }
            }
            item {
                Text("Legendas", style = MaterialTheme.typography.titleMedium)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Mostrar", Modifier.weight(1f)); Switch(captionsOn, { captionsOn = it })
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("CAIXA ALTA", Modifier.weight(1f)); Switch(uppercase, { uppercase = it }, enabled = captionsOn)
                }
                Text("Cor da palavra ativa", style = MaterialTheme.typography.bodyMedium)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    HIGHLIGHTS.forEach { hex ->
                        Box(Modifier.size(36.dp).background(Color(android.graphics.Color.parseColor(hex)), CircleShape)
                            .border(if (hex.equals(highlight, true)) 3.dp else 1.dp, MaterialTheme.colorScheme.onSurface, CircleShape)
                            .clickable(enabled = captionsOn) { highlight = hex })
                    }
                }
                Text("Altura na tela: ${(position * 100).toInt()}%", style = MaterialTheme.typography.bodyMedium)
                Slider(position, { position = it }, valueRange = 0.15f..0.88f, enabled = captionsOn)
            }
            item {
                Text("Texto da legenda (toque para corrigir)", style = MaterialTheme.typography.titleMedium)
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    words.forEach { (i, w) ->
                        val text = edits[i] ?: w.text
                        Text(text, Modifier
                            .background(if (edits.containsKey(i)) MaterialTheme.colorScheme.tertiaryContainer
                                else MaterialTheme.colorScheme.surfaceVariant, MaterialTheme.shapes.small)
                            .clickable(enabled = captionsOn) { editing = i }
                            .padding(horizontal = 6.dp, vertical = 3.dp))
                    }
                }
            }
            item {
                if (error.isNotBlank()) Text(error, color = MaterialTheme.colorScheme.error)
                Button(enabled = !saving, modifier = Modifier.fillMaxWidth().padding(vertical = 12.dp), onClick = { save() }) {
                    Text(if (saving) "Salvando…" else "Salvar e gerar prévia")
                }
            }
        }
    }

    editing?.let { idx ->
        val original = transcript?.words?.getOrNull(idx)?.text.orEmpty()
        var value by remember(idx) { mutableStateOf(edits[idx] ?: original) }
        AlertDialog(onDismissRequest = { editing = null },
            title = { Text("Corrigir palavra") },
            text = { OutlinedTextField(value, { value = it }, singleLine = true) },
            confirmButton = { TextButton(onClick = {
                if (value == original) edits.remove(idx) else edits[idx] = value
                editing = null
            }) { Text("OK") } },
            dismissButton = { TextButton(onClick = { edits.remove(idx); editing = null }) { Text("Restaurar") } })
    }
}
