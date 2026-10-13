package com.clipstudio.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import com.clipstudio.app.data.AppGraph

/** Navegação simples por pilha de rotas ("projects", "settings", "project/<id>", "clip/<id>", "edit/<id>"). */
@Composable
fun ClipStudioApp() {
    val api by AppGraph.api.collectAsState()
    var stack by rememberSaveable { mutableStateOf(arrayListOf(if (api == null) "settings" else "projects")) }
    fun push(route: String) { stack = ArrayList(stack + route) }
    fun pop() { if (stack.size > 1) stack = ArrayList(stack.dropLast(1)) }
    fun replace(route: String) { stack = arrayListOf(route) }

    BackHandler(enabled = stack.size > 1) { pop() }

    val route = stack.last()
    val client = api
    if (client == null || route == "settings") {
        SettingsScreen(onDone = { if (stack.size > 1) pop() else replace("projects") })
        return
    }
    val arg = route.substringAfter('/', "")
    when (route.substringBefore('/')) {
        "projects" -> ProjectsScreen(client, onOpen = { push("project/$it") }, onSettings = { push("settings") })
        "project" -> ProjectScreen(client, arg, onClip = { push("clip/$it") }, onBack = ::pop)
        "clip" -> ClipScreen(client, arg, onEdit = { push("edit/$arg") }, onBack = ::pop)
        "edit" -> EditorScreen(client, arg, onSaved = ::pop, onBack = ::pop)
        else -> replace("projects")
    }
}
