param([switch]$Worker)
$ErrorActionPreference = 'Stop'
$installRoot = Join-Path $env:LOCALAPPDATA 'BlackboardSync\program'
$installLog = Join-Path $env:LOCALAPPDATA 'BlackboardSync\install.log'
$installResult = Join-Path $env:LOCALAPPDATA 'BlackboardSync\install-result.txt'
New-Item -ItemType Directory -Force (Split-Path $installLog) | Out-Null

if ($Worker) {
    try {
        Start-Transcript -Path $installLog -Force | Out-Null
        Write-Host 'Checking Python 3.11 or newer with desktop support...'
        $pythonExe = $null
        $candidates = @()
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($py) {
            $found = & $py.Source -3 -c 'import sys, tkinter, venv; assert sys.version_info >= (3,11); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += ($found | Select-Object -Last 1) }
        }
        $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCmd -and $pythonCmd.Source -notlike '*WindowsApps*') { $candidates += $pythonCmd.Source }
        $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
        foreach ($candidate in $candidates) {
            & $candidate -c 'import sys, tkinter, venv; assert sys.version_info >= (3,11)' 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonExe = $candidate; break }
        }
        if (-not $pythonExe) {
            Write-Host 'Installing Python for your Windows account...'
            if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw 'Windows App Installer / winget is missing. Install Python 3.11+ from python.org, then run this installer again.' }
            & winget.exe install --id Python.Python.3.12 --exact --source winget --scope user --silent --accept-package-agreements --accept-source-agreements
            $candidate = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
            if (-not (Test-Path $candidate)) { throw 'Python installation did not complete. Install Python 3.11+ from python.org, then retry.' }
            $pythonExe = $candidate
        }
        Write-Host 'Copying the app...'
        New-Item -ItemType Directory -Force $installRoot | Out-Null
        foreach ($name in @('app.py','bbsync','LICENSE','USER-GUIDE.txt','CREDITS.txt','README.txt','CHANGELOG.txt','Start-App.cmd')) {
            Copy-Item (Join-Path $PSScriptRoot $name) $installRoot -Recurse -Force
        }
        Write-Host 'Creating a private Python environment...'
        & $pythonExe -m venv (Join-Path $installRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the app environment.' }
        $runtime = Join-Path $installRoot '.venv\Scripts\python.exe'
        Write-Host 'Installing browser automation...'
        & $runtime -m pip install 'playwright>=1.40,<2'
        if ($LASTEXITCODE -ne 0) { throw 'Dependency download failed. Check internet and retry.' }
        Write-Host 'Downloading the login browser...'
        & $runtime -m playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Browser download failed. Check internet and retry.' }
        Write-Host 'Creating desktop and Start Menu shortcuts...'
        $shell = New-Object -ComObject WScript.Shell
        foreach ($location in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
            $shortcut = $shell.CreateShortcut((Join-Path $location 'Blackboard Sync.lnk'))
            $shortcut.TargetPath = Join-Path $installRoot '.venv\Scripts\pythonw.exe'
            $shortcut.Arguments = '"' + (Join-Path $installRoot 'app.py') + '"'
            $shortcut.WorkingDirectory = $installRoot
            $shortcut.Description = 'Sync Blackboard courses to your chosen folders'
            $shortcut.Save()
        }
        'SUCCESS' | Set-Content $installResult
        Write-Host 'Installed. Open Blackboard Sync from your desktop.'
    } catch {
        ('FAILED: ' + $_.Exception.Message) | Set-Content $installResult
        Write-Host $_.Exception.Message
    } finally { Stop-Transcript -ErrorAction SilentlyContinue | Out-Null }
    exit
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text='Install Blackboard Sync'; $form.Size=New-Object System.Drawing.Size(680,510)
$form.StartPosition='CenterScreen'
$title=New-Object System.Windows.Forms.Label
$title.Text='Blackboard Sync'; $title.Font=New-Object System.Drawing.Font('Segoe UI',20,[System.Drawing.FontStyle]::Bold)
$title.SetBounds(20,18,600,40);$form.Controls.Add($title)
$description=New-Object System.Windows.Forms.Label
$description.Text="Checks Python, installs required components, and creates a desktop shortcut.`nInternet required. No Blackboard login or course downloads during installation.`nDeveloped by Riyan Hashem Jamy."
$description.SetBounds(22,65,620,62);$form.Controls.Add($description)
$box=New-Object System.Windows.Forms.TextBox
$box.Multiline=$true;$box.ReadOnly=$true;$box.ScrollBars='Vertical';$box.SetBounds(22,130,620,265);$form.Controls.Add($box)
$install=New-Object System.Windows.Forms.Button
$install.Text='Install';$install.SetBounds(22,410,140,35);$form.Controls.Add($install)
$launch=New-Object System.Windows.Forms.Button
$launch.Text='Open App';$launch.Enabled=$false;$launch.SetBounds(175,410,140,35);$form.Controls.Add($launch)
$script:proc=$null
$timer=New-Object System.Windows.Forms.Timer;$timer.Interval=800
$timer.Add_Tick({
    if(Test-Path $installLog){$box.Text=(Get-Content $installLog -Raw -ErrorAction SilentlyContinue);$box.SelectionStart=$box.Text.Length;$box.ScrollToCaret()}
    if($script:proc -and $script:proc.HasExited){
        $timer.Stop();$install.Enabled=$true
        $result=Get-Content $installResult -Raw -ErrorAction SilentlyContinue
        if($result -like 'SUCCESS*'){$launch.Enabled=$true;$install.Text='Reinstall'}
        else {[System.Windows.Forms.MessageBox]::Show($result,'Installation stopped')|Out-Null}
    }
})
$install.Add_Click({
    $install.Enabled=$false;$launch.Enabled=$false
    Remove-Item $installResult -ErrorAction SilentlyContinue
    $box.Text='Starting installation...'
    $arguments='-NoProfile -ExecutionPolicy Bypass -File "'+$PSCommandPath+'" -Worker'
    $script:proc=Start-Process powershell.exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $timer.Start()
})
$launch.Add_Click({Start-Process (Join-Path $installRoot '.venv\Scripts\pythonw.exe') -ArgumentList ('"'+(Join-Path $installRoot 'app.py')+'"');$form.Close()})
$form.Add_FormClosing({param($sender,$eventArgs) if($script:proc -and -not $script:proc.HasExited){$eventArgs.Cancel=$true;[System.Windows.Forms.MessageBox]::Show('Wait for installation to finish.','Installation running')|Out-Null}})
[void]$form.ShowDialog()
