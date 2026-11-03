Get-CimInstance Win32_Process | Where-Object {$_.Name -like '*python*'} | Select-Object ProcessId,CommandLine | Format-Table -AutoSize
