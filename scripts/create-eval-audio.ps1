param([string]$OutputDir = "logs/evaluation/audio")
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$root = [System.IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Force -Path $root | Out-Null
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $synth.SelectVoice("Microsoft Huihui Desktop")
  $synth.Rate = 2
  $texts = [ordered]@{
    complete = "请帮我搜索项目文档里面关于语音识别的说明。"
    prefix = "我想查一下"
    suffix = "项目文档里面关于语音识别的说明。"
    correction = "帮我查一下明天的安排，不对，是后天上午三点以后的安排。"
    long = ("请帮我查看项目文档，先找到语音识别的说明，再确认结束判断的处理方式，最后整理需要修改的内容。" * 5)
  }
  $manifest = @()
  foreach ($name in $texts.Keys) {
    $path = Join-Path $root "$name.wav"
    $synth.SetOutputToWaveFile($path)
    $synth.Speak($texts[$name])
    $synth.SetOutputToNull()
    $manifest += @{ name=$name; path=$path; reference=$texts[$name]; source="synthetic Microsoft Huihui Desktop" }
  }
  $manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $root "manifest.json")
} finally { $synth.Dispose() }
