<#
.SYNOPSIS  Generate the Inno Setup wizard branding bitmaps (1x and 2x) from handback\assets\icon-256.png.
.PARAMETER OutDir  Folder that receives wizard-banner-*.bmp and wizard-small-*.bmp.
#>
param([Parameter(Mandatory)][string]$OutDir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$repo = Split-Path $PSScriptRoot -Parent
$logo = [Drawing.Image]::FromFile((Join-Path $repo 'handback\assets\icon-256.png'))
$bg = [Drawing.ColorTranslator]::FromHtml('#1f1f1f')
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function New-Canvas([int]$w, [int]$h) {
    $bmp = New-Object Drawing.Bitmap $w, $h, ([Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'HighQuality'; $g.InterpolationMode = 'HighQualityBicubic'
    $g.PixelOffsetMode = 'HighQuality'; $g.TextRenderingHint = 'AntiAliasGridFit'
    $g.Clear($bg)
    return @($bmp, $g)
}
function Save-Bmp($bmp, $g, [string]$name) {
    $g.Dispose()
    $bmp.Save((Join-Path $OutDir $name), [Drawing.Imaging.ImageFormat]::Bmp)
    $bmp.Dispose()
}

foreach ($s in 1, 2) {
    $bmp, $g = New-Canvas (164 * $s) (314 * $s)
    $size = 96 * $s
    $g.DrawImage($logo, [int]((164 * $s - $size) / 2), [int](100 * $s), $size, $size)
    $font = New-Object Drawing.Font 'Segoe UI Semibold', (15 * $s), ([Drawing.FontStyle]::Regular), ([Drawing.GraphicsUnit]::Pixel)
    $fmt = New-Object Drawing.StringFormat; $fmt.Alignment = 'Center'
    $rect = New-Object Drawing.RectangleF 0, ([single](208 * $s)), ([single](164 * $s)), ([single](30 * $s))
    $g.DrawString('handback', $font, [Drawing.Brushes]::White, $rect, $fmt)
    Save-Bmp $bmp $g "wizard-banner-${s}x.bmp"

    $bmp, $g = New-Canvas (55 * $s) (55 * $s)
    $size = 43 * $s
    $g.DrawImage($logo, [int]((55 * $s - $size) / 2), [int]((55 * $s - $size) / 2), $size, $size)
    Save-Bmp $bmp $g "wizard-small-${s}x.bmp"
}
$logo.Dispose()