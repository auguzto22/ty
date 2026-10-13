package com.clipstudio.app.data

import android.content.Context
import android.content.SharedPreferences
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** Configuração do servidor (URL + token) e o cliente correspondente. Objeto único do processo. */
object AppGraph {
    private lateinit var prefs: SharedPreferences
    private val _api = MutableStateFlow<ApiClient?>(null)
    val api: StateFlow<ApiClient?> = _api

    fun init(context: Context) {
        if (::prefs.isInitialized) return
        prefs = context.applicationContext.getSharedPreferences("clipstudio", Context.MODE_PRIVATE)
        rebuild()
    }

    val serverUrl: String get() = prefs.getString("server_url", "").orEmpty()
    val token: String get() = prefs.getString("token", "").orEmpty()

    /** E-mail da conta logada (vazio = token manual de administrador). */
    val accountEmail: String get() = prefs.getString("account_email", "").orEmpty()

    fun save(serverUrl: String, token: String, accountEmail: String = "") {
        prefs.edit().putString("server_url", serverUrl.trim().trimEnd('/')).putString("token", token.trim())
            .putString("account_email", accountEmail).apply()
        rebuild()
    }

    private fun rebuild() {
        _api.value = serverUrl.takeIf { it.startsWith("http") }?.let { ApiClient(it, token) }
    }

    /** Guarda os dados do upload para poder reenviar se o worker falhar de vez. */
    fun rememberUpload(projectId: String, uploadUrl: String, uri: String, size: Long, title: String) {
        prefs.edit().putString("upload_$projectId", listOf(uploadUrl, uri, size.toString(), title).joinToString("\u0001")).apply()
    }

    fun pendingUpload(projectId: String): PendingUpload? {
        val parts = prefs.getString("upload_$projectId", null)?.split("\u0001") ?: return null
        if (parts.size < 4) return null
        return PendingUpload(projectId, parts[0], parts[1], parts[2].toLongOrNull() ?: 0, parts[3])
    }

    fun forgetUpload(projectId: String) = prefs.edit().remove("upload_$projectId").apply()

    data class PendingUpload(val projectId: String, val uploadUrl: String, val uri: String, val size: Long, val title: String)

    /** Cliente para workers (o processo pode ter sido recriado). */
    fun requireApi(context: Context): ApiClient {
        init(context)
        return _api.value ?: throw IllegalStateException("Servidor não configurado")
    }
}
