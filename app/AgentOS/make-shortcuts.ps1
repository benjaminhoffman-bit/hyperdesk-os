# make-shortcuts.ps1 - HyperDesk and Home Base shortcuts (Start menu + desktop) with the SILVADES HyperDesk icon.
# The Start-menu HyperDesk shortcut also carries the AppUserModelID SILVADES.HyperDesk: that is how Windows ties
# Agent OS notifications to HyperDesk's name and logo (a click should launch this shortcut).
# User scope, no admin, no registry. Safe to run again. Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\make-shortcuts.ps1
# A shortcut whose script is not next to this file is skipped. Lights goes in the Start menu only (few PCs have RGB).
param([string]$Pythonw = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe")
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) { throw "pythonw.exe not found at $Pythonw. Install Python 3.12, or pass -Pythonw with its path." }
$icon = "$here\assets\hyperdesk.ico"
$start = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
$desktop = [Environment]::GetFolderPath('Desktop')

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
  [PreserveSig] int GetCount(out uint count);
  [PreserveSig] int GetAt(uint index, out PropertyKey key);
  [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
  [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
  [PreserveSig] int Commit();
}
[StructLayout(LayoutKind.Sequential, Pack = 4)] public struct PropertyKey { public Guid Fmtid; public uint Pid; }
[StructLayout(LayoutKind.Explicit)] public struct PropVariant { [FieldOffset(0)] public ushort Vt; [FieldOffset(8)] public IntPtr Ptr; }
public static class AppId {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
  static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bindCtx, int flags, ref Guid iid, out IPropertyStore store);
  public static void Set(string lnk, string id) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    IPropertyStore store;
    Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2 /* GPS_READWRITE */, ref iid, out store));
    PropertyKey key = new PropertyKey { Fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Pid = 5 };  // System.AppUserModel.ID
    PropVariant value = new PropVariant { Vt = 31 /* VT_LPWSTR */, Ptr = Marshal.StringToCoTaskMemUni(id) };
    try { Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value)); Marshal.ThrowExceptionForHR(store.Commit()); }
    finally { Marshal.FreeCoTaskMem(value.Ptr); Marshal.ReleaseComObject(store); }
  }
}
'@

$shell = New-Object -ComObject WScript.Shell
function Link($path, $script, $text, $ico = $icon) {
  if (-not (Test-Path -LiteralPath "$here\$script")) { return }
  $l = $shell.CreateShortcut($path)
  $l.TargetPath = $Pythonw
  $l.Arguments = "`"$here\$script`""
  $l.WorkingDirectory = $here
  $l.IconLocation = "$ico,0"
  $l.Description = $text
  $l.Save()
  "ok  $path"
}
foreach ($dir in @($start, $desktop)) {
  Link "$dir\HyperDesk.lnk" 'hyperdesk.pyw' 'HyperDesk by SILVADES: agents, models, projects and services in one window'
  Link "$dir\HyperDesk Home Base.lnk" 'homebase.pyw' 'HyperDesk Home Base (the compact Windows app)'
  Link "$dir\HyperDesk Game Mode.lnk" 'gamemode.pyw' 'HyperDesk Game Mode: close all but Discord, music and games' "$here\assets\gamemode.ico"
}
Link "$start\HyperDesk Lights.lnk" 'lights.pyw' 'HyperDesk Lights: one color for the RGB OpenRGB reaches'
[AppId]::Set("$start\HyperDesk.lnk", 'SILVADES.HyperDesk')
"ok  AppUserModelID SILVADES.HyperDesk on $start\HyperDesk.lnk"
