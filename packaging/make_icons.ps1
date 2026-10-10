# Regenerates handback/assets/icon-*.png and icon.ico from assets/source/logo-lineart.png
# (black line-art hand on transparent) as a white glyph on a #2563EB rounded-square tile.
# Usage: powershell -File packaging/make_icons.ps1 [-Variant A|B] [-PreviewOnly]
#   A = white outlines, hand interior = tile blue.   B = white-filled hand with blue detail lines.
param([ValidateSet('A','B')][string]$Variant = 'A', [switch]$PreviewOnly, [string]$OutDir = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$root = Split-Path -Parent $PSScriptRoot
$assets = Join-Path $root 'handback\assets'
$srcDir = Join-Path $assets 'source'
if (-not $OutDir) { $OutDir = $assets }
$tile = [Drawing.Color]::FromArgb(255, 0x25, 0x63, 0xEB)

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System; using System.Drawing; using System.Drawing.Imaging; using System.Collections.Generic;
public static class Glyph {
  // Stroke mask [0..1], cropped to bbox, padded square, resampled to N x N.
  public static float[] LoadMask(string path, int N) {
    Bitmap b = new Bitmap(path); int w=b.Width,h=b.Height; float[] m=new float[w*h];
    int x0=w,y0=h,x1=-1,y1=-1;
    for(int y=0;y<h;y++)for(int x=0;x<w;x++){ Color c=b.GetPixel(x,y);
      float lum=(0.299f*c.R+0.587f*c.G+0.114f*c.B)/255f; float v=(c.A/255f)*(1f-lum); m[y*w+x]=v;
      if(v>0.5f){ if(x<x0)x0=x; if(x>x1)x1=x; if(y<y0)y0=y; if(y>y1)y1=y; } }
    b.Dispose();
    int cw=x1-x0+1, ch=y1-y0+1, side=Math.Max(cw,ch); float[] o=new float[N*N];
    float sc=(float)(N-2)/side; float ox=(N-cw*sc)/2f, oy=(N-ch*sc)/2f;
    for(int y=0;y<N;y++)for(int x=0;x<N;x++){
      float sx=(x-ox)/sc+x0, sy=(y-oy)/sc+y0;
      int ix=(int)Math.Floor(sx-0.5f), iy=(int)Math.Floor(sy-0.5f); float fx=sx-0.5f-ix, fy=sy-0.5f-iy; float acc=0;
      for(int dy=0;dy<2;dy++)for(int dx=0;dx<2;dx++){ int px=ix+dx,py=iy+dy; float v=(px<0||py<0||px>=w||py>=h)?0:m[py*w+px];
        acc+=v*(dx==0?1-fx:fx)*(dy==0?1-fy:fy);} o[y*N+x]=acc; }
    return o;
  }
  public static float[] Dilate(float[] m,int N,int r){
    if(r<=0) return (float[])m.Clone(); float[] o=new float[N*N];
    for(int y=0;y<N;y++)for(int x=0;x<N;x++){ float best=0;
      for(int dy=-r;dy<=r;dy++)for(int dx=-r;dx<=r;dx++){ if(dx*dx+dy*dy>r*r)continue; int px=x+dx,py=y+dy;
        if(px<0||py<0||px>=N||py>=N)continue; float v=m[py*N+px]; if(v>best)best=v; } o[y*N+x]=best; }
    return o;
  }
  public static Bitmap RenderA(float[] m,int N){ Bitmap b=new Bitmap(N,N,PixelFormat.Format32bppArgb);
    for(int y=0;y<N;y++)for(int x=0;x<N;x++){ int a=(int)Math.Round(Math.Min(1f,m[y*N+x])*255); b.SetPixel(x,y,Color.FromArgb(a,255,255,255)); } return b; }
  // White-filled hand silhouette, hand strokes blue, arrow (stroke component with no enclosed interior) white.
  public static Bitmap RenderB(float[] m,int N,Color blue){
    bool[] stroke=new bool[N*N]; for(int i=0;i<N*N;i++) stroke[i]=m[i]>0.5f;
    bool[] outside=new bool[N*N]; Stack<int> st=new Stack<int>();
    for(int k=0;k<N;k++){ Push(outside,stroke,st,N,k,0);Push(outside,stroke,st,N,k,N-1);Push(outside,stroke,st,N,0,k);Push(outside,stroke,st,N,N-1,k);}
    while(st.Count>0){int i=st.Pop(); int x=i%N,y=i/N; Push(outside,stroke,st,N,x+1,y);Push(outside,stroke,st,N,x-1,y);Push(outside,stroke,st,N,x,y+1);Push(outside,stroke,st,N,x,y-1);}
    bool[] interior=new bool[N*N]; for(int i=0;i<N*N;i++) interior[i]=!outside[i]&&!stroke[i];
    bool[] hand=new bool[N*N];
    for(int s=0;s<N*N;s++){ if(!stroke[s]||hand[s])continue;
      List<int> comp=new List<int>(); bool touches=false; Stack<int> cs=new Stack<int>(); cs.Push(s); hand[s]=true;
      while(cs.Count>0){int i=cs.Pop(); comp.Add(i); int x=i%N,y=i/N;
        for(int dy=-1;dy<=1;dy++)for(int dx=-1;dx<=1;dx++){int px=x+dx,py=y+dy; if(px<0||py<0||px>=N||py>=N)continue; int j=py*N+px;
          if(interior[j])touches=true; if(stroke[j]&&!hand[j]){hand[j]=true;cs.Push(j);} } }
      if(!touches) foreach(int i in comp) hand[i]=false; }
    Bitmap b=new Bitmap(N,N,PixelFormat.Format32bppArgb);
    for(int y=0;y<N;y++)for(int x=0;x<N;x++){ int i=y*N+x; float v=Math.Min(1f,m[i]);
      bool nearHand=false; if(!stroke[i]&&v>0){ for(int dy=-2;dy<=2&&!nearHand;dy++)for(int dx=-2;dx<=2;dx++){int px=x+dx,py=y+dy;if(px<0||py<0||px>=N||py>=N)continue;if(hand[py*N+px]){nearHand=true;break;}} }
      if(hand[i]||nearHand){
        int r=(int)(255*(1-v)+blue.R*v), g=(int)(255*(1-v)+blue.G*v), bl=(int)(255*(1-v)+blue.B*v);
        b.SetPixel(x,y,Color.FromArgb(255,r,g,bl)); }
      else if(interior[i]) b.SetPixel(x,y,Color.FromArgb(255,255,255,255));
      else b.SetPixel(x,y,Color.FromArgb((int)(v*255),255,255,255)); }
    return b; }
  static void Push(bool[] outside,bool[] stroke,Stack<int> st,int N,int x,int y){
    if(x<0||y<0||x>=N||y>=N)return; int i=y*N+x; if(outside[i]||stroke[i])return; outside[i]=true; st.Push(i); }
}
'@

function New-Tile([int]$s, [string]$variant, [float[]]$mask, [int]$maskN) {
  $r = switch ($s) { 16 {22} 20 {17} 24 {13} 32 {8} 40 {4} 48 {2} default {0} }
  $ss = 4; $BS = $s * $ss
  $big = New-Object Drawing.Bitmap $BS, $BS, ([Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [Drawing.Graphics]::FromImage($big); $g.SmoothingMode = 'AntiAlias'; $g.Clear([Drawing.Color]::Transparent)
  $rad = 0.22 * $BS * 2
  $p = New-Object Drawing.Drawing2D.GraphicsPath
  $p.AddArc(0, 0, $rad, $rad, 180, 90); $p.AddArc($BS-$rad, 0, $rad, $rad, 270, 90)
  $p.AddArc($BS-$rad, $BS-$rad, $rad, $rad, 0, 90); $p.AddArc(0, $BS-$rad, $rad, $rad, 90, 90); $p.CloseFigure()
  $g.FillPath((New-Object Drawing.SolidBrush $tile), $p); $g.Dispose()
  $out = New-Object Drawing.Bitmap $s, $s, ([Drawing.Imaging.PixelFormat]::Format32bppArgb)
  $g = [Drawing.Graphics]::FromImage($out); $g.InterpolationMode = 'HighQualityBicubic'; $g.PixelOffsetMode = 'HighQuality'
  $g.CompositingQuality = 'HighQuality'; $g.DrawImage($big, 0, 0, $s, $s)
  $m = [Glyph]::Dilate($mask, $maskN, $r)
  if ($variant -eq 'A') { $gl = [Glyph]::RenderA($m, $maskN) } else { $gl = [Glyph]::RenderB($m, $maskN, $tile) }
  if ($s -le 24) { $frac = 0.76 } else { $frac = 0.70 }; $gs = [int][math]::Round($s * $frac); $off = [int][math]::Round(($s - $gs) / 2)
  $g.DrawImage($gl, (New-Object Drawing.Rectangle $off, $off, $gs, $gs), 0, 0, $maskN, $maskN, [Drawing.GraphicsUnit]::Pixel)
  $g.Dispose(); $gl.Dispose(); $big.Dispose(); $p.Dispose()
  return ,$out
}

$logo = Join-Path $srcDir 'logo-lineart.png'
$maskN = 512
$mask = [Glyph]::LoadMask($logo, $maskN)
$sizes = 16,20,24,32,40,48,64,128,256

if ($PreviewOnly) {
  foreach ($v in 'A','B') {
    $b = New-Tile 256 $v $mask $maskN; $b.Save((Join-Path $srcDir ("preview-" + $v.ToLower() + ".png")), [Drawing.Imaging.ImageFormat]::Png); $b.Dispose()
  }
  $sheet = New-Object Drawing.Bitmap 900, 400; $g = [Drawing.Graphics]::FromImage($sheet); $g.Clear([Drawing.Color]::FromArgb(32,32,32))
  $g.InterpolationMode = 'NearestNeighbor'; $g.PixelOffsetMode = 'Half'
  $row = 0
  foreach ($v in 'A','B') { $x = 10
    foreach ($s in 16,24,32) { $b = New-Tile $s $v $mask $maskN; $g.DrawImage($b, $x, 10 + $row*200, $s*5, $s*5); $g.DrawImage($b, $x + $s*5 + 4, 10 + $row*200 + 5*$s - $s, $s, $s); $x += $s*5 + 60; $b.Dispose() }
    $row++ }
  $sheet.Save((Join-Path $srcDir 'compare-ab.png')); $g.Dispose(); $sheet.Dispose()
  return
}

$pngs = @{}
foreach ($s in $sizes) {
  $b = New-Tile $s $Variant $mask $maskN
  $path = Join-Path $OutDir ("icon-$s.png"); $b.Save($path, [Drawing.Imaging.ImageFormat]::Png)
  $pngs[$s] = [IO.File]::ReadAllBytes($path); $b.Dispose()
}
$ms = New-Object IO.MemoryStream; $bw = New-Object IO.BinaryWriter $ms
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
foreach ($s in $sizes) {
  if ($s -ge 256) { $d = 0 } else { $d = $s }
  $bw.Write([byte]$d); $bw.Write([byte]$d); $bw.Write([byte]0); $bw.Write([byte]0)
  $bw.Write([uint16]1); $bw.Write([uint16]32); $bw.Write([uint32]$pngs[$s].Length); $bw.Write([uint32]$offset)
  $offset += $pngs[$s].Length
}
foreach ($s in $sizes) { $bw.Write($pngs[$s]) }
$bw.Flush(); [IO.File]::WriteAllBytes((Join-Path $OutDir 'icon.ico'), $ms.ToArray())
Write-Output "wrote $($sizes.Count) sizes + icon.ico to $OutDir (variant $Variant)"
