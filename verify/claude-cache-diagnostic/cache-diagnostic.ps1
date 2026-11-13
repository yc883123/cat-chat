[CmdletBinding()]
param(
    [string]$ConfigPath = '',
    [string]$ProviderName = '',
    [string]$Model = '',
    [ValidateSet('configured', 'both', 'claude', 'chat')][string]$Mode = 'configured',
    [string]$OutputDirectory = '',
    [string]$CatChatVersion = '',
    [ValidateRange(5, 180)][int]$TimeoutSeconds = 60,
    [switch]$Yes,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$script:SecretValues = New-Object 'System.Collections.Generic.List[string]'
$script:ModeWasSupplied = $PSBoundParameters.ContainsKey('Mode')
$script:ProbeVersion = '1.0.0'
$script:ParserSnapshot = 'Cat Chat protocols.py / _online_usage，2026-11-13 源码快照'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http

function Get-Field($Object, [string]$Name, $Fallback = $null) {
    if ($null -eq $Object) { return $Fallback }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
    } elseif ($null -ne $Object.PSObject.Properties[$Name]) {
        return $Object.PSObject.Properties[$Name].Value
    }
    return $Fallback
}

function Protect-Text($Value) {
    $text = [string]$Value
    foreach ($secret in $script:SecretValues) {
        if ($secret.Length -gt 0) {
            $text = $text.Replace($secret, '[已隐藏]')
            $escaped = ($secret | ConvertTo-Json -Compress)
            if ($escaped.Length -gt 2) { $text = $text.Replace($escaped.Substring(1, $escaped.Length - 2), '[已隐藏]') }
        }
    }
    $text = [regex]::Replace($text, '(?i)Bearer\s+[^\s,";<>]+', 'Bearer [已隐藏]')
    $text = [regex]::Replace($text, '(?i)(?:sk-|sk_)[A-Za-z0-9_\-]{6,}', '[密钥已隐藏]')
    $text = [regex]::Replace($text, '(?im)(authorization|x-api-key|api[_-]?key|access[_-]?token|set-cookie|cookie)\s*["'']?\s*[:=]\s*[^\r\n]+', '$1: [已隐藏]')
    $text = [regex]::Replace($text, '(?i)[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\s"<>]+', '[本机路径已隐藏]')
    $text = [regex]::Replace($text, '[\x00-\x08\x0b\x0c\x0e-\x1f]', '')
    if ($text.Length -gt 600) { $text = $text.Substring(0, 600) + '…' }
    return $text
}

function Get-Hash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-Endpoint([string]$BaseUrl, [string]$Route) {
    $uri = $null
    if (-not [Uri]::TryCreate($BaseUrl.Trim(), [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @('http', 'https')) {
        throw 'API 地址必须是完整的 http:// 或 https:// 地址。'
    }
    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw '此诊断包不支持在地址内携带账号、查询参数或片段；请使用普通 API 基础地址和独立 API Key。'
    }
    $base = $uri.AbsoluteUri.TrimEnd('/')
    $base = [regex]::Replace($base, '/(?:chat/completions|messages(?:/count_tokens)?|responses|models)$', '', 'IgnoreCase')
    if ($base -notmatch '/v1$') { $base += '/v1' }
    return $base + '/' + $Route
}

function New-ProbeClient($Config, [string]$Endpoint) {
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $handler.UseCookies = $false
    $handler.AutomaticDecompression = [Net.DecompressionMethods]::GZip -bor [Net.DecompressionMethods]::Deflate
    $hostName = ([Uri]$Endpoint).DnsSafeHost
    $local = $hostName -in @('localhost', '127.0.0.1', '::1') -or $hostName -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'
    $proxy = Get-Field $Config 'proxy'
    $policy = 'system'
    if ($local -or ($null -ne $proxy -and -not (Get-Field $proxy 'enabled' $false))) {
        $handler.UseProxy = $false
        $policy = 'direct'
    } elseif ($null -ne $proxy) {
        $proxyUrl = [string](Get-Field $proxy 'url' '')
        if ($proxyUrl) {
            $proxyUri = $null
            if (-not [Uri]::TryCreate($proxyUrl, [UriKind]::Absolute, [ref]$proxyUri) -or $proxyUri.Scheme -notin @('http', 'https')) {
                throw '手动代理地址无效，支持 http:// 和 https://。请在 Cat Chat 修正后重试。'
            }
            $script:SecretValues.Add($proxyUrl)
            $handler.Proxy = New-Object Net.WebProxy($proxyUri)
            $handler.UseProxy = $true
            $policy = 'manual'
        } elseif (-not (Get-Field $proxy 'use_system_fallback' $true)) {
            $handler.UseProxy = $false
            $policy = 'direct'
        }
    }
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [Threading.Timeout]::InfiniteTimeSpan
    $client.MaxResponseContentBufferSize = 2097152
    return @{ Client = $client; Policy = $policy }
}

function Wait-ProbeTask($Task, [Diagnostics.Stopwatch]$Clock, [int]$Limit) {
    $remaining = [int][Math]::Max(0, $Limit * 1000 - $Clock.ElapsedMilliseconds)
    if ($remaining -le 0 -or -not $Task.Wait($remaining)) { throw '请求超过总时间限制，已停止等待；不会自动重试。' }
    return $Task.GetAwaiter().GetResult()
}

function Read-ProbeUsage($Object) {
    $out = [ordered]@{}
    if ($null -eq $Object) { return $out }
    foreach ($key in @('input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'cached_tokens', 'prompt_cache_hit_tokens', 'claude_cache_creation_5_m_tokens', 'claude_cache_creation_1_h_tokens')) {
        $value = Get-Field $Object $key
        $number = 0L
        if ($null -ne $value -and [long]::TryParse([string]$value, [ref]$number) -and $number -ge 0) { $out[$key] = $number }
    }
    foreach ($key in @('prompt_tokens_details', 'input_tokens_details', 'cache_creation')) {
        $source = Get-Field $Object $key
        $details = [ordered]@{}
        foreach ($part in @('cached_tokens', 'cached_creation_tokens', 'cache_write_tokens', 'ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens')) {
            $number = 0L
            $value = Get-Field $source $part
            if ($null -ne $value -and [long]::TryParse([string]$value, [ref]$number) -and $number -ge 0) { $details[$part] = $number }
        }
        if ($details.Count) { $out[$key] = $details }
    }
    $billing = Get-Field (Get-Field $Object 'billing_usage') 'claude_usage'
    if ($null -ne $billing) { $out['billing_claude_usage'] = Read-ProbeUsage $billing }
    return $out
}

function Get-Number($Usage, [string[]]$Names) {
    foreach ($key in $Names) {
        $value = Get-Field $Usage $key
        if ($null -ne $value) { return [long]$value }
    }
    return 0L
}

function Get-NormalizedUsage($Usage, [string]$Format, [switch]$CurrentParser) {
    $inputCount = Get-Number $Usage @('prompt_tokens', 'input_tokens')
    $outputCount = Get-Number $Usage @('completion_tokens', 'output_tokens')
    $cached = Get-Number $Usage @('cached_tokens', 'cache_read_input_tokens', 'prompt_cache_hit_tokens')
    $written = Get-Number $Usage @('cache_creation_input_tokens')
    $hasRead = $null -ne (Get-Field $Usage 'cache_read_input_tokens') -or $null -ne (Get-Field $Usage 'cached_tokens') -or $null -ne (Get-Field $Usage 'prompt_cache_hit_tokens')
    if ($Format -eq 'claude') { $inputCount += $cached + $written }
    foreach ($key in @('prompt_tokens_details', 'input_tokens_details')) {
        $detail = Get-Field $Usage $key
        if ($null -ne (Get-Field $detail 'cached_tokens')) {
            $hasRead = $true
            $cached = [Math]::Max($cached, [long](Get-Field $detail 'cached_tokens' 0))
        }
        $written = [Math]::Max($written, (Get-Number $detail @('cached_creation_tokens', 'cache_write_tokens')))
    }
    if ($CurrentParser -and $inputCount -gt 0) { $cached = [Math]::Min($cached, $inputCount) }
    $percent = $null
    if ($inputCount -gt 0 -and $hasRead) { $percent = [Math]::Round(100.0 * $cached / $inputCount, 3) }
    return [ordered]@{
        has_usage = ($Usage.Count -gt 0); has_cache_read_field = $hasRead
        input_tokens = $inputCount; output_tokens = $outputCount
        cached_tokens = $cached; cache_creation_input_tokens = $written
        cache_hit_percent = $percent
    }
}

function Get-ErrorSummary($Value) {
    $errorObject = Get-Field $Value 'error' $Value
    $message = Get-Field $errorObject 'message'
    if ($null -eq $message -and $errorObject -is [string]) { $message = $errorObject }
    if ($null -eq $message) { $message = '服务端返回错误，未保存原始响应正文。' }
    return Protect-Text $message
}

function Add-ResponseEvent($State, [string]$JsonText) {
    if (-not $JsonText.Trim()) { return }
    if ($JsonText.Trim() -eq '[DONE]') { $State.Done = $true; return }
    try { $chunk = $JsonText | ConvertFrom-Json } catch { $State.InvalidJson += 1; return }
    $eventType = [string](Get-Field $chunk 'type' (Get-Field $chunk 'object' 'json'))
    $knownEvents = @('message_start', 'message_delta', 'message_stop', 'content_block_start', 'content_block_delta', 'content_block_stop', 'ping', 'error', 'message', 'chat.completion.chunk', 'chat.completion', 'json')
    if ($eventType -notin $knownEvents) { $eventType = 'unknown' }
    if ($State.Events.Count -lt 25 -and -not $State.Events.Contains($eventType)) { $State.Events.Add($eventType) }
    $errorObject = Get-Field $chunk 'error'
    if ($null -ne $errorObject -or $eventType -eq 'error') {
        $State.Error = Get-ErrorSummary $chunk
        $State.Done = $true
        return
    }
    $message = Get-Field $chunk 'message'
    $responseId = [string](Get-Field $message 'id' (Get-Field $chunk 'id' ''))
    if ($responseId -and -not $State.IdHash) { $State.IdHash = Get-Hash $responseId }
    foreach ($location in @('message.usage', 'usage')) {
        $source = if ($location -eq 'message.usage') { Get-Field $message 'usage' } else { Get-Field $chunk 'usage' }
        if ($null -ne $source) {
            $usage = Read-ProbeUsage $source
            if ($State.UsageEvents.Count -lt 50) {
                $State.UsageEvents.Add([ordered]@{ event = $eventType; location = $location; usage = $usage })
            }
            foreach ($key in $usage.Keys) { $State.Merged[$key] = $usage[$key] }
            if ($location -eq 'usage' -and $usage.Count) { $State.LastTopLevel = $usage }
        }
    }
    if ($eventType -eq 'message_stop') { $State.Done = $true }
    $stopReason = Get-Field (Get-Field $chunk 'delta') 'stop_reason'
    if ($stopReason) { $State.Finish = Protect-Text $stopReason }
    $delta = Get-Field $chunk 'delta'
    if ((Get-Field $delta 'type') -eq 'text_delta') { $State.AnswerChars += ([string](Get-Field $delta 'text' '')).Length }
    foreach ($choice in @(Get-Field $chunk 'choices' @())) {
        $finish = Get-Field $choice 'finish_reason'
        if ($finish) { $State.Finish = Protect-Text $finish }
        $State.AnswerChars += ([string](Get-Field (Get-Field $choice 'delta') 'content' '')).Length
    }
    if ($eventType -eq 'message' -or $eventType -eq 'chat.completion') {
        $State.Done = $true
        $State.IsJson = $true
    }
}

function Invoke-ProbeHttp($Client, [string]$Endpoint, [string]$ApiKey, [string]$Format, $Payload, [switch]$ListModels) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $cancel = New-Object Threading.CancellationTokenSource
    $cancel.CancelAfter($TimeoutSeconds * 1000)
    $method = if ($ListModels) { [Net.Http.HttpMethod]::Get } else { [Net.Http.HttpMethod]::Post }
    $request = New-Object Net.Http.HttpRequestMessage($method, $Endpoint)
    $null = $request.Headers.TryAddWithoutValidation('User-Agent', 'Mozilla/5.0 CatChatCacheDiagnostic/1.0')
    if ($Format -eq 'claude') {
        $null = $request.Headers.TryAddWithoutValidation('anthropic-version', '2023-06-01')
        if ($ApiKey) { $null = $request.Headers.TryAddWithoutValidation('x-api-key', $ApiKey) }
    } elseif ($ApiKey) { $null = $request.Headers.TryAddWithoutValidation('Authorization', 'Bearer ' + $ApiKey) }
    if (-not $ListModels) {
        $json = $Payload | ConvertTo-Json -Depth 30 -Compress
        $request.Content = New-Object Net.Http.StringContent($json, [Text.Encoding]::UTF8, 'application/json')
    }
    $state = @{
        Done = $false; InvalidJson = 0; Error = ''; IdHash = ''; Finish = ''; AnswerChars = 0; IsJson = $false
        Events = (New-Object 'System.Collections.Generic.List[string]')
        UsageEvents = (New-Object 'System.Collections.Generic.List[object]')
        Merged = [ordered]@{}; LastTopLevel = [ordered]@{}
    }
    $result = [ordered]@{ http_status = 0; elapsed_seconds = 0; error = ''; content_type = ''; transport_complete = $false }
    $response = $null
    $reader = $null
    $raw = New-Object Text.StringBuilder
    try {
        $task = $Client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token)
        $response = Wait-ProbeTask $task $clock $TimeoutSeconds
        $result.http_status = [int]$response.StatusCode
        $result.content_type = [string]$response.Content.Headers.ContentType.MediaType
        if ($response.Content.Headers.ContentLength -gt 2097152) { throw '响应超过诊断包的 2 MB 上限，已停止读取。' }
        $stream = Wait-ProbeTask ($response.Content.ReadAsStreamAsync()) $clock $TimeoutSeconds
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        $dataLines = New-Object 'System.Collections.Generic.List[string]'
        $sse = $result.content_type -eq 'text/event-stream' -and $result.http_status -ge 200 -and $result.http_status -lt 300 -and -not $ListModels
        while ($true) {
            $line = Wait-ProbeTask ($reader.ReadLineAsync()) $clock $TimeoutSeconds
            if ($null -eq $line) { break }
            $null = $raw.AppendLine($line)
            if ($raw.Length -gt 2097152) { throw '响应超过诊断包的 2 MB 上限，已停止读取。' }
            if ($sse) {
                if ($line.StartsWith('data:')) { $dataLines.Add($line.Substring(5).TrimStart()) }
                if ($line.Length -eq 0 -and $dataLines.Count) {
                    Add-ResponseEvent $state ($dataLines -join "`n")
                    $dataLines.Clear()
                    if ($state.Done) { break }
                }
            }
        }
        if ($sse -and $dataLines.Count -and -not $state.Done) { Add-ResponseEvent $state ($dataLines -join "`n") }
        if ($result.http_status -lt 200 -or $result.http_status -ge 300) {
            try { $result.error = Get-ErrorSummary ($raw.ToString() | ConvertFrom-Json) }
            catch { $result.error = '服务端返回非 JSON 错误页；未保存该页面。' }
            if ($result.http_status -ge 300 -and $result.http_status -lt 400) { $result.error = '接口发生重定向；为避免把密钥转发到其他地址，诊断已停止。' }
        } elseif ($ListModels) {
            $catalog = $raw.ToString() | ConvertFrom-Json
            $rows = Get-Field $catalog 'data' (Get-Field $catalog 'models' @())
            $ids = @($rows | ForEach-Object { if ($_ -is [string]) { $_ } else { [string](Get-Field $_ 'id' (Get-Field $_ 'name' '')) } } | Where-Object { $_ } | Select-Object -Unique)
            $result['model_ids'] = $ids
        } elseif (-not $sse) { Add-ResponseEvent $state $raw.ToString() }
        $result.transport_complete = $true
    } catch {
        if ($cancel.IsCancellationRequested -or $clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            $result.error = '请求超时；已停止等待，不自动重试。'
        } else {
            $result.error = Protect-Text ('连接或读取失败：' + $_.Exception.Message)
        }
    } finally {
        $cancel.Cancel()
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
        $request.Dispose()
        $cancel.Dispose()
        $clock.Stop()
    }
    $result.elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 3)
    if (-not $ListModels) {
        if ($state.Error) { $result.error = $state.Error }
        $result['usage_events'] = @($state.UsageEvents.ToArray())
        $result['merged_server_usage'] = $state.Merged
        $result['server_usage'] = Get-NormalizedUsage $state.Merged $Format
        $current = if ($state.IsJson) { $state.Merged } else { $state.LastTopLevel }
        $result['cat_chat_snapshot_usage'] = Get-NormalizedUsage $current $Format -CurrentParser
        $result['response_id_sha256'] = $state.IdHash
        $result['response_body_sha256'] = Get-Hash $raw.ToString()
        $result['events_seen'] = @($state.Events.ToArray())
        $result['invalid_json_events'] = $state.InvalidJson
        $result['finish_reason'] = $state.Finish
        $result['saw_done'] = $state.Done
        $result['answer_characters'] = $state.AnswerChars
        $result['success'] = ($result.http_status -ge 200 -and $result.http_status -lt 300 -and -not $result.error -and $state.Done)
        if (-not $result.error -and -not $state.Done) { $result.error = '响应没有完整结束标记，无法作为完整用量证据；本组不再发第二次。' }
    }
    return $result
}

function Find-Config {
    if ($ConfigPath) { return $ConfigPath.Trim('"') }
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += Join-Path $env:LOCALAPPDATA 'NaibaChat\config.json' }
    $candidates += Join-Path $PSScriptRoot 'config.json'
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate } }
    if ($Yes) { throw '未找到 Cat Chat 配置，请用 -ConfigPath 指定 config.json。' }
    Write-Host '未找到默认配置。请将 Cat Chat 的 config.json 拖入此窗口，再按回车。'
    return (Read-Host '配置文件路径').Trim('"')
}

function Select-Profile($Config) {
    $providers = @(Get-Field $Config 'providers' @())
    $available = @($providers | Where-Object {
        (Get-Field $_ 'kind' 'online') -eq 'online' -and (Get-Field $_ 'request_format' 'openai_chat') -in @('claude', 'openai_chat')
    })
    if (-not $available.Count) { throw '配置里没有 Chat 或 Claude 格式的在线 API。' }
    if ($ProviderName) {
        $matches = @($available | Where-Object { (Get-Field $_ 'name') -eq $ProviderName -or (Get-Field $_ 'id') -eq $ProviderName })
        if ($matches.Count -ne 1) { throw '未找到唯一匹配的 API，请检查 -ProviderName。' }
        return $matches[0]
    }
    if ($Yes) { throw '无人值守运行时必须用 -ProviderName 指定 API。' }
    Write-Host ''
    for ($i = 0; $i -lt $available.Count; $i++) {
        $p = $available[$i]
        Write-Host ('{0}. {1}  [{2}]  {3}' -f ($i + 1), (Protect-Text (Get-Field $p 'name')), (Get-Field $p 'request_format'), (Protect-Text (Get-Field $p 'model')))
    }
    $number = 0
    if (-not [int]::TryParse((Read-Host '输入要测试的 API 序号'), [ref]$number) -or $number -lt 1 -or $number -gt $available.Count) { throw 'API 序号无效。' }
    return $available[$number - 1]
}

function Get-RunFindings($Runs) {
    $findings = New-Object 'System.Collections.Generic.List[string]'
    foreach ($format in @('claude', 'openai_chat')) {
        $group = @($Runs | Where-Object { $_.format -eq $format })
        if (-not $group.Count) { continue }
        $label = if ($format -eq 'claude') { 'Claude' } else { 'Chat' }
        foreach ($row in $group) {
            if (-not $row.success) {
                $findings.Add(('{0} 第 {1} 次未成功：HTTP {2}，{3}。没有据此判断缓存。' -f $label, $row.round, $row.http_status, $row.error))
                continue
            }
            $server = $row.server_usage
            $client = $row.cat_chat_snapshot_usage
            if ($server.input_tokens -ne $client.input_tokens -or $server.cached_tokens -ne $client.cached_tokens) {
                $findings.Add(('{0} 第 {1} 次：服务端用量与当前 Cat Chat 解析快照不一致。服务端输入/缓存读取={2}/{3}，解析快照={4}/{5}。' -f $label, $row.round, $server.input_tokens, $server.cached_tokens, $client.input_tokens, $client.cached_tokens))
            }
            if (-not $server.has_cache_read_field) {
                $findings.Add(('{0} 第 {1} 次没有可识别的缓存读取字段，不能把它算成零命中。' -f $label, $row.round))
            } elseif ($server.cached_tokens -gt 0) {
                $ratio = if ($null -ne $server.cache_hit_percent) { [string]$server.cache_hit_percent + '%' } else { '总输入未知，比例不可算' }
                $findings.Add(('{0} 第 {1} 次：服务端报告缓存读取 {2} token，比例 {3}。' -f $label, $row.round, $server.cached_tokens, $ratio))
            } else {
                $findings.Add(('{0} 第 {1} 次服务端报告缓存读取为 0；仅凭这一小组不能断定供应商不支持缓存。' -f $label, $row.round))
            }
            if ($server.cached_tokens -gt $server.input_tokens) { $findings.Add('缓存读取大于总输入，用量语义异常，需要检查中转字段。') }
        }
        if ($group.Count -eq 2 -and $group[0].response_id_sha256 -and $group[0].response_id_sha256 -eq $group[1].response_id_sha256) {
            $findings.Add($label + ' 两次不同请求返回同一响应 ID，疑似响应复用或上游 ID 异常，不能据此证明新的前缀缓存命中。')
        }
    }
    return $findings.ToArray()
}

function Save-Report($Report, [string]$Folder) {
    $Report.findings = @(Get-RunFindings $Report.runs)
    $json = $Report | ConvertTo-Json -Depth 40
    # Report fields are allowlisted. The exact selected credential is also checked before any write.
    foreach ($secret in $script:SecretValues) {
        if ($secret.Length -ge 8 -and $json.Contains($secret)) { throw '报告检查发现敏感内容，已停止保存；请勿发送窗口截图。' }
    }
    $utf8 = New-Object Text.UTF8Encoding($true)
    [IO.File]::WriteAllText((Join-Path $Folder 'report.json'), $json, $utf8)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add('Cat Chat 缓存诊断报告')
    $lines.Add('时间：' + $Report.started_at)
    $lines.Add('API：' + $Report.provider.name + '；模型：' + $Report.provider.model)
    $lines.Add('地址：' + $Report.provider.base_url + '；配置原格式：' + $Report.provider.configured_format)
    $lines.Add('用户填写的 Cat Chat 版本：' + $Report.cat_chat_version)
    $lines.Add('代理策略：' + $Report.proxy_policy)
    $lines.Add('')
    foreach ($row in $Report.runs) {
        $lines.Add(('{0} 第{1}次：HTTP {2}，耗时 {3}s，完整结束={4}' -f $row.format, $row.round, $row.http_status, $row.elapsed_seconds, $row.saw_done))
        $lines.Add(('  服务端：总输入={0}，缓存读={1}，缓存写={2}，输出={3}，读取比例={4}' -f $row.server_usage.input_tokens, $row.server_usage.cached_tokens, $row.server_usage.cache_creation_input_tokens, $row.server_usage.output_tokens, $(if ($null -eq $row.server_usage.cache_hit_percent) { '未知' } else { [string]$row.server_usage.cache_hit_percent + '%' })))
        $lines.Add(('  Cat Chat 解析快照：输入={0}，缓存读={1}' -f $row.cat_chat_snapshot_usage.input_tokens, $row.cat_chat_snapshot_usage.cached_tokens))
        if ($row.error) { $lines.Add('  错误：' + $row.error) }
    }
    $lines.Add('')
    foreach ($finding in $Report.findings) { $lines.Add('* ' + $finding) }
    $lines.Add('')
    foreach ($note in $Report.limitations) { $lines.Add($note) }
    $lines.Add('报告不含密钥、鉴权头、Cookie、配置文件、真实聊天记录或原始生成内容。')
    [IO.File]::WriteAllText((Join-Path $Folder 'report.txt'), ($lines -join "`r`n"), $utf8)
}

function Main {
    Write-Host 'Cat Chat 缓存诊断 1.0' -ForegroundColor Cyan
    Write-Host '使用人工测试文本，不读取聊天。每种格式最多 2 次生成请求，失败不自动重试。'
    $path = Find-Config
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw '找不到指定配置文件。' }
    $script:SecretValues.Add([IO.Path]::GetFullPath($path))
    $config = [IO.File]::ReadAllText([IO.Path]::GetFullPath($path), [Text.Encoding]::UTF8) | ConvertFrom-Json
    foreach ($p in @(Get-Field $config 'providers' @())) {
        $key = [string](Get-Field $p 'api_key' '')
        if ($key) { $script:SecretValues.Add($key) }
    }
    $accessToken = [string](Get-Field $config 'access_token' '')
    if ($accessToken) { $script:SecretValues.Add($accessToken) }
    $provider = Select-Profile $config
    $apiKey = [string](Get-Field $provider 'api_key' '')
    $configuredFormat = [string](Get-Field $provider 'request_format' 'openai_chat')
    $baseUrl = [string](Get-Field $provider 'base_url' '')
    $catalogEndpoint = Get-Endpoint $baseUrl 'models'
    $clientInfo = New-ProbeClient $config $catalogEndpoint
    $client = $clientInfo.Client
    try {
        $chosenModel = $Model
        if (-not $chosenModel) {
            $chosenModel = [string](Get-Field $provider 'model' '')
            if (-not $Yes) {
                Write-Host ('当前模型：' + (Protect-Text $chosenModel))
                $answer = Read-Host '回车沿用；输入模型名替换；输入 ? 查询可用模型'
                if ($answer -eq '?') {
                    $catalog = Invoke-ProbeHttp $client $catalogEndpoint $apiKey $configuredFormat $null -ListModels
                    if ($catalog.error) { Write-Host ('模型列表读取失败：' + $catalog.error) -ForegroundColor Yellow }
                    else { foreach ($id in @(Get-Field $catalog 'model_ids' @())) { Write-Host ('  ' + (Protect-Text $id)) } }
                    $answer = Read-Host '输入模型名，回车沿用当前模型'
                }
                if ($answer.Trim()) { $chosenModel = $answer.Trim() }
            }
        }
        if (-not $chosenModel) { throw '模型名不能为空。' }
        if (-not $Yes -and -not $script:ModeWasSupplied) {
            Write-Host '1. 仅测试当前格式（2 次请求）'
            Write-Host '2. 对照 Claude 和 Chat（4 次请求，适合反馈缓存问题）'
            $selection = Read-Host '选择测试方式，回车选 1'
            if ($selection -eq '2') { $Mode = 'both' }
            elseif ($selection -and $selection -ne '1') { throw '测试方式无效。' }
        }
        if (-not $Yes -and -not $CatChatVersion) { $CatChatVersion = Read-Host 'Cat Chat 版本号（可留空）' }
        $formats = switch ($Mode) {
            'both' { @('claude', 'openai_chat') }
            'claude' { @('claude') }
            'chat' { @('openai_chat') }
            default { @($configuredFormat) }
        }
        $formats = @($formats)
        $maximumRequests = $formats.Count * 2
        Write-Host ''
        Write-Host ('准备测试：{0} / {1}' -f (Protect-Text (Get-Field $provider 'name')), (Protect-Text $chosenModel))
        Write-Host ('最多 {0} 次生成；每次约 6,000 输入 token，最多 256 输出 token，按供应商规则计费。' -f $maximumRequests) -ForegroundColor Yellow
        Write-Host '不修改 Cat Chat 配置。测试结束后只需发回 report.zip。'
        if (-not $Yes -and (Read-Host '确认开始请输入 Y') -notin @('Y', 'y')) { Write-Host '已取消，未发送生成请求。'; return }
        if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot 'reports' }
        $null = [IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($OutputDirectory))
        $runId = [Guid]::NewGuid().ToString('N')
        $folder = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) ((Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $runId.Substring(0, 8))
        $null = [IO.Directory]::CreateDirectory($folder)
        $report = [ordered]@{
            schema_version = 1; diagnostic_version = $script:ProbeVersion; run_id = $runId
            started_at = (Get-Date).ToString('o'); cat_chat_version = (Protect-Text $CatChatVersion)
            parser_snapshot = $script:ParserSnapshot; proxy_policy = $clientInfo.Policy
            provider = [ordered]@{ name = (Protect-Text (Get-Field $provider 'name')); base_url = (Protect-Text $baseUrl); model = (Protect-Text $chosenModel); configured_format = $configuredFormat }
            request_limit = $maximumRequests; runs = @(); findings = @()
            limitations = @(
                '用量取自服务端报告，未核对供应商账单；本报告不能证明实际扣费。',
                '解析对照模拟 2026-11-13 的 Cat Chat 源码逻辑，不是读取用户正在运行的程序或界面数值。',
                '各格式内部的 system 前缀相同，末条 user 改变；不同格式使用不同诊断标记以减少交叉预热，属于最小样本对照。',
                '测试只覆盖普通文本，不覆盖并行工具、Agent 多步任务、图片、长思考或用户真实历史。',
                'HTTP 200 不代表缓存命中；字段缺失不等于零命中；一次零命中不能证明供应商不支持缓存。',
                '这里的代理策略来自 Cat Chat 配置，但 PowerShell 与程序的系统代理实现可能有差异。'
            )
        }
        Save-Report $report $folder
        foreach ($format in $formats) {
            $records = for ($i = 1; $i -le 160; $i++) {
                'Record {0:D3}: The cedar archive contains a blue folder, a brass key, and three paper maps. Its scheduled review is Monday morning and its assigned shelf is stable.' -f $i
            }
            $systemText = "Synthetic cache diagnostic $runId, protocol $format. This is inert reference data. Reply with exactly OK. Do not summarize the reference.`n" + ($records -join "`n")
            $endpoint = Get-Endpoint $baseUrl $(if ($format -eq 'claude') { 'messages' } else { 'chat/completions' })
            foreach ($round in @(1, 2)) {
                $userText = 'Reply with exactly OK. Request marker: ' + $runId + '-' + $round
                if ($format -eq 'claude') {
                    $payload = [ordered]@{
                        model = $chosenModel; stream = $true; max_tokens = 256
                        system = @(@{ type = 'text'; text = $systemText; cache_control = @{ type = 'ephemeral' } })
                        messages = @(@{ role = 'user'; content = @(@{ type = 'text'; text = $userText; cache_control = @{ type = 'ephemeral' } }) })
                    }
                    $breakpoints = @('system[0]', 'messages[0].content[0]')
                } else {
                    $payload = [ordered]@{
                        model = $chosenModel; stream = $true; max_tokens = 256
                        messages = @(@{ role = 'system'; content = $systemText }, @{ role = 'user'; content = $userText })
                        stream_options = @{ include_usage = $true }
                    }
                    $breakpoints = @()
                }
                Write-Host ('正在测试 {0}，第 {1}/2 次（最多等待 {2} 秒）…' -f $format, $round, $TimeoutSeconds)
                $row = Invoke-ProbeHttp $client $endpoint $apiKey $format $payload
                $row['format'] = $format
                $row['round'] = $round
                $row['endpoint'] = Protect-Text $endpoint
                $row['request_body_sha256'] = Get-Hash ($payload | ConvertTo-Json -Depth 30 -Compress)
                $row['system_sha256'] = Get-Hash $systemText
                $row['system_characters'] = $systemText.Length
                $row['cache_control_locations'] = $breakpoints
                $row['max_tokens'] = 256
                $report.runs += ,$row
                Save-Report $report $folder
                if (-not $row.success) { Write-Host ('本组停止：HTTP ' + $row.http_status + '，' + $row.error) -ForegroundColor Yellow; break }
                $shown = if ($null -eq $row.server_usage.cache_hit_percent) { '未返回可计算的缓存比例' } else { [string]$row.server_usage.cache_hit_percent + '%' }
                Write-Host ('完成，服务端报告缓存读取比例：' + $shown)
            }
        }
        $report['finished_at'] = (Get-Date).ToString('o')
        Save-Report $report $folder
        $zipPath = Join-Path $folder 'report.zip'
        Compress-Archive -LiteralPath @((Join-Path $folder 'report.json'), (Join-Path $folder 'report.txt')) -DestinationPath $zipPath
        Write-Host ''
        Write-Host '测试结束。请把下面这个 report.zip 发回，勿发送 config.json：' -ForegroundColor Green
        Write-Host $zipPath
        foreach ($finding in $report.findings) { Write-Host ('  ' + $finding) }
        if (-not $NoPause) { Start-Process explorer.exe -ArgumentList ('"' + $folder + '"') }
    } finally { $client.Dispose() }
}

$exitCode = 0
try { Main }
catch {
    $exitCode = 1
    Write-Host ('诊断停止：' + (Protect-Text $_.Exception.Message)) -ForegroundColor Red
    Write-Host '若已经产生 report.zip 或 report.json，可以发回；不要发送配置文件或密钥。'
}
finally { if (-not $NoPause) { $null = Read-Host '按回车关闭窗口' } }
exit $exitCode
