# Finds the Windows Terminal tab whose title contains $Title, used by claude_monitor.pyw.
#   -Action select (default): selects the tab and prints the handle of the window hosting it.
#   -Action close: closes the tab, but only if EXACTLY ONE tab matches. With several matches
#                  nothing is closed, so a wrong tab (another session) can never be hit.
# Prints nothing if no tab is found (or, for close, if the match is ambiguous).
param([int]$WtPid, [string]$Title, [string]$Action = 'select')
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [Windows.Automation.AutomationElement]
$byPid = New-Object Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $WtPid)
$isTab = New-Object Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [Windows.Automation.ControlType]::TabItem)
$isButton = New-Object Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [Windows.Automation.ControlType]::Button)
$pattern = "*" + [Management.Automation.WildcardPattern]::Escape($Title.Trim()) + "*"

$matches = @()
foreach ($win in $AE::RootElement.FindAll('Children', $byPid)) {
    foreach ($tab in $win.FindAll('Descendants', $isTab)) {
        if ($tab.Current.Name -like $pattern) { $matches += , @($win, $tab) }
    }
}
if ($matches.Count -eq 0) { return }

if ($Action -eq 'close') {
    if ($matches.Count -ne 1) { return }
    $tab = $matches[0][1]
    # The close button is the only button inside the tab item.
    $button = $tab.FindFirst('Descendants', $isButton)
    if ($button) {
        $button.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
        'closed'
    }
    return
}

$win, $tab = $matches[0]
$tab.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
$win.Current.NativeWindowHandle
