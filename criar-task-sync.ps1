# Cria a task agendada do sync (SATLBASE -> Postgres), a cada 6h, invisivel (S4U + pythonw).
# Precisa rodar em PowerShell ELEVADO (clique direito > Executar como administrador) —
# criar task com LogonType S4U exige UAC, Claude não consegue elevar sozinho.

$Action = New-ScheduledTaskAction -Execute "C:\Users\Renato\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" `
  -Argument '"C:\Users\Renato\scripts-clavis\comparador-cotacoes-simples\sync_comparador_simples.py"'
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date -Hour 6 -Minute 30 -Second 0) `
  -RepetitionInterval (New-TimeSpan -Hours 6) -RepetitionDuration (New-TimeSpan -Days 365)
$Principal = New-ScheduledTaskPrincipal -UserId "Renato" -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName "ComparadorSimplesSyncSATLBASE" -Action $Action -Trigger $Trigger `
  -Principal $Principal `
  -Description "Sync SATLBASE -> Postgres (comparador de cotacoes simples), a cada 6h. Roda invisivel (S4U + pythonw)." `
  -Force

Get-ScheduledTask -TaskName "ComparadorSimplesSyncSATLBASE" | Select-Object TaskName, State
