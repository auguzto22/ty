package com.clipstudio.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.clipstudio.app.data.ApiClient
import com.clipstudio.app.data.ApiException
import com.clipstudio.app.data.AppGraph
import kotlinx.coroutines.launch

@Composable
fun SettingsScreen(onDone: () -> Unit) {
    var url by remember { mutableStateOf(AppGraph.serverUrl.ifBlank { "http://192.168.0.10:8000" }) }
    var useAccount by remember { mutableStateOf(AppGraph.token.isBlank() || AppGraph.accountEmail.isNotBlank()) }
    var email by remember { mutableStateOf(AppGraph.accountEmail) }
    var password by remember { mutableStateOf("") }
    var token by remember { mutableStateOf(if (AppGraph.accountEmail.isBlank()) AppGraph.token else "") }
    var status by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun describe(e: Exception) = when {
        e is ApiException && e.code == 401 -> "E-mail, senha ou token incorretos"
        e is ApiException -> e.message ?: "erro ${e.code}"
        else -> "Sem conexão: ${e.message}"
    }

    Column(Modifier.fillMaxSize().padding(16.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Servidor ClipStudio", style = MaterialTheme.typography.titleLarge)
        Text("Endereço do servidor (IP do computador na rede local ou https://seu-dominio).",
            style = MaterialTheme.typography.bodyMedium)
        OutlinedTextField(url, { url = it }, label = { Text("Endereço") }, singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), modifier = Modifier.fillMaxWidth())
        if (AppGraph.accountEmail.isNotBlank()) {
            Text("Conectado como ${AppGraph.accountEmail}", style = MaterialTheme.typography.bodySmall)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(selected = useAccount, onClick = { useAccount = true }, label = { Text("Conta") })
            FilterChip(selected = !useAccount, onClick = { useAccount = false }, label = { Text("Token de administrador") })
        }
        if (useAccount) {
            OutlinedTextField(email, { email = it.trim() }, label = { Text("E-mail") }, singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email), modifier = Modifier.fillMaxWidth())
            OutlinedTextField(password, { password = it }, label = { Text("Senha") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(enabled = !busy && url.startsWith("http") && email.contains("@") && password.isNotBlank(), onClick = {
                    busy = true
                    status = "Entrando…"
                    scope.launch {
                        try {
                            val session = ApiClient(url, "").login(email, password)
                            AppGraph.save(url, session.token, session.user.email)
                            password = ""
                            onDone()
                        } catch (e: Exception) {
                            status = describe(e)
                        } finally {
                            busy = false
                        }
                    }
                }) { Text("Entrar") }
                OutlinedButton(enabled = !busy && url.startsWith("http") && email.contains("@") && password.length >= 8, onClick = {
                    busy = true
                    status = "Criando conta…"
                    scope.launch {
                        try {
                            val session = ApiClient(url, "").register(email, password)
                            AppGraph.save(url, session.token, session.user.email)
                            password = ""
                            onDone()
                        } catch (e: Exception) {
                            status = if (e is ApiException && e.code == 403) "O servidor não aceita cadastro; peça uma conta ao administrador"
                            else describe(e)
                        } finally {
                            busy = false
                        }
                    }
                }) { Text("Criar conta") }
            }
        } else {
            OutlinedTextField(token, { token = it }, label = { Text("API_TOKEN") }, singleLine = true,
                visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = {
                    status = "Testando…"
                    scope.launch {
                        status = try {
                            val h = ApiClient(url, token).health()
                            ApiClient(url, token).listProjects()
                            "Conectado · rostos: ${if (h.faces) "sim" else "não"} · IA: ${h.llm}"
                        } catch (e: Exception) {
                            describe(e)
                        }
                    }
                }) { Text("Testar conexão") }
                Button(onClick = { AppGraph.save(url, token); onDone() }, enabled = url.startsWith("http")) { Text("Salvar") }
            }
        }
        if (status.isNotBlank()) Text(status, style = MaterialTheme.typography.bodyMedium)
    }
}
