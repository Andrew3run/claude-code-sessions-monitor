# Selects the Windows Terminal tab whose title contains $Title and prints the handle of the window
# that hosts it. Prints nothing if the tab is not found. Used by claude_monitor.pyw.
param([int]$WtPid, [string]$Title)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [Windows.Automation.AutomationElement]
$byPid = New-Object Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $WtPid)
$isTab = New-Object Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [Windows.Automation.ControlType]::TabItem)
$pattern = "*" + [Management.Automation.WildcardPattern]::Escape($Title.Trim()) + "*"
foreach ($win in $AE::RootElement.FindAll('Children', $byPid)) {
    foreach ($tab in $win.FindAll('Descendants', $isTab)) {
        if ($tab.Current.Name -like $pattern) {
            $tab.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern).Select()
            $win.Current.NativeWindowHandle
            exit
        }
    }
}
