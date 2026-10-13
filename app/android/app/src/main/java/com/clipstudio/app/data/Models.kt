package com.clipstudio.app.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** Contrato JSON da API do backend (app/backend/app/api/main.py). Campos desconhecidos são ignorados. */
val ApiJson = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = true
}

@Serializable
data class Health(
    val ok: Boolean,
    val llm: String = "off",
    val faces: Boolean = false,
    val auth: Boolean = false,
    val signup: Boolean = false,
)

@Serializable
data class Credentials(val email: String, val password: String)

@Serializable
data class SessionUser(val id: String, val email: String, @SerialName("is_admin") val isAdmin: Boolean = false)

@Serializable
data class LoginResponse(val token: String, val user: SessionUser)

@Serializable
data class Usage(val projects: Int = 0, val minutes: Double = 0.0)

@Serializable
data class Quota(@SerialName("projects_per_day") val projectsPerDay: Int = 0, @SerialName("minutes_per_day") val minutesPerDay: Double = 0.0)

@Serializable
data class Me(
    val id: String? = null,
    val email: String = "",
    @SerialName("is_admin") val isAdmin: Boolean = false,
    @SerialName("usage_24h") val usage: Usage = Usage(),
    val quota: Quota? = null,
)

@Serializable
data class CaptionStyle(
    val enabled: Boolean = true,
    val font: String = "",
    @SerialName("size_ratio") val sizeRatio: Double = 0.065,
    val color: String = "#FFFFFF",
    @SerialName("highlight_color") val highlightColor: String = "#FFE135",
    @SerialName("stroke_color") val strokeColor: String = "#000000",
    @SerialName("stroke_ratio") val strokeRatio: Double = 0.12,
    val position: Double = 0.72,
    val uppercase: Boolean = true,
    @SerialName("max_words") val maxWords: Int = 3,
    @SerialName("max_chars") val maxChars: Int = 18,
)

@Serializable
data class RenderPrefs(
    val aspect: String = "9:16",
    val reframe: String = "auto",
    val captions: CaptionStyle = CaptionStyle(),
)

@Serializable
data class ProjectPrefs(
    @SerialName("min_duration") val minDuration: Double = 20.0,
    @SerialName("max_duration") val maxDuration: Double = 60.0,
    @SerialName("max_clips") val maxClips: Int = 3,
    val prompt: String = "",
    val keywords: List<String> = emptyList(),
    val language: String = "",
    val render: RenderPrefs = RenderPrefs(),
)

@Serializable
data class CreateProjectRequest(
    val title: String,
    val filename: String,
    val size: Long,
    @SerialName("content_type") val contentType: String,
    val prefs: ProjectPrefs,
)

@Serializable
data class UploadTarget(val id: String, val url: String, @SerialName("chunk_size") val chunkSize: Long)

@Serializable
data class CreateProjectResponse(val project: Project, val upload: UploadTarget)

@Serializable
data class Project(
    val id: String,
    val title: String = "",
    val status: String = "",
    val stage: String = "",
    val progress: Double = 0.0,
    val message: String = "",
    val error: String = "",
    val duration: Double = 0.0,
    val language: String = "",
    val curation: String = "",
    val warnings: List<String> = emptyList(),
    @SerialName("queue_position") val queuePosition: Int? = null,
    @SerialName("created_at") val createdAt: String = "",
) {
    val isBusy get() = status in setOf("CREATED", "UPLOADING", "UPLOADED", "QUEUED", "PROCESSING")
    val isDone get() = status == "COMPLETE"
    val isFailed get() = status == "FAILED"
}

@Serializable
data class ProjectEnvelope(val project: Project)

@Serializable
data class MediaRef(val url: String, val version: Int, val stale: Boolean = false)

@Serializable
data class ClipScores(
    val hook: Double = 0.0,
    val coherence: Double = 0.0,
    val value: Double = 0.0,
    val trend: Double = 0.0,
    val audio: Double = 0.0,
    val visual: Double = 0.0,
)

@Serializable
data class QaReport(val ok: Boolean = false, val problems: List<String> = emptyList())

@Serializable
data class Clip(
    val id: String,
    @SerialName("project_id") val projectId: String,
    val rank: Int,
    val score: Double = 0.0,
    val scores: ClipScores = ClipScores(),
    val title: String = "",
    val hook: String = "",
    val reason: String = "",
    val start: Double,
    val end: Double,
    val duration: Double,
    val aspect: String = "9:16",
    val reframe: String = "auto",
    val captions: CaptionStyle = CaptionStyle(),
    @SerialName("word_overrides") val wordOverrides: Map<String, String> = emptyMap(),
    val version: Int = 1,
    @SerialName("render_pending") val renderPending: Boolean = false,
    val preview: MediaRef? = null,
    val export: MediaRef? = null,
    @SerialName("thumbnail_url") val thumbnailUrl: String? = null,
    val qa: QaReport = QaReport(),
) {
    /** Export pronto para a versão atual do corte. */
    val exportReady get() = export != null && !export.stale
}

@Serializable
data class ClipEdit(
    val start: Double? = null,
    val end: Double? = null,
    val aspect: String? = null,
    val reframe: String? = null,
    val captions: CaptionStyle? = null,
    @SerialName("word_overrides") val wordOverrides: Map<Int, String>? = null,
)

@Serializable
data class Word(val text: String, val start: Double, val end: Double)

@Serializable
data class Transcript(val language: String = "", val duration: Double = 0.0, val words: List<Word> = emptyList())
