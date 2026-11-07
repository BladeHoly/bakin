# UI smoke test: builds a debug copy of Bakin that can be driven over the
# Chrome DevTools Protocol, runs it on throwaway data and checks the views,
# dialogs, the link menu, the virtual library and the player (packaging\ui-smoke.mjs).
# Exits non-zero on any failure; release.ps1 runs it before every release.
#
# The copy cannot touch the installed Bakin: its own identifier (so it is not
# "the running Bakin" for the single-instance check), no bakin:// registration,
# and its own data folder (BAKIN_HOME) and WebView2 profile, deleted afterwards.
#
#   -Only <regex>   run only the checks whose name matches (an area, e.g. 'C9|C10|bandeja'
#                   or 'Configuración'); the build and the seeding still happen (PLAN 0.23.3)
param([string]$Only = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$port = 9231
$work = Join-Path $env:TEMP ("bakin-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$home_ = Join-Path $work 'home'
$seed = Join-Path $work 'Fotos de prueba'
New-Item -ItemType Directory -Force $home_, $seed | Out-Null
# its own download folder (never the user's Downloads, Windows' default), with a music folder
# where a song is still downloading: its cover sits next to it and must not show in Fotos (C66)
$dl = Join-Path $work 'dl'
$musicDl = Join-Path $dl 'Música'
New-Item -ItemType Directory -Force $musicDl | Out-Null
'{"preset": "Música"}' | Set-Content -Encoding utf8 (Join-Path $musicDl '.bakin-media.json')
@{ out_dir = $dl } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $home_ 'ui-settings.json')
# a followed list whose last check brought 2, had 1 and failed 1 (C97); checked just now, so the
# scheduler leaves it alone (no network)
& $py -c "import json, sys, time; t = time.time(); json.dump({'_v': 1, 'follows': [{'id': 'smoke1', 'kind': 'media', 'url': 'https://www.youtube.com/playlist?list=PLsmoke', 'title': 'Lista seguida de prueba', 'preset': 'bimusic', 'every_hours': 168, 'last_check': t, 'enabled': True, 'history': [{'at': t - 60, 'outcome': 'ok', 'new': 2, 'skipped': 1, 'failed': ['El video es privado.']}]}]}, open(sys.argv[1], 'w', encoding='utf-8'))" (Join-Path $home_ 'follows.json')
& $py -c "import sys; from PIL import Image; Image.new('RGB', (40, 40), (200, 40, 40)).save(sys.argv[1] + '/Tema que baja.webp'); Image.new('RGB', (64, 36), (40, 200, 40)).save(sys.argv[1] + '/Portada huerfana [dQw4w9WgXcQ].webp'); open(sys.argv[1] + '/Tema que baja.f251.webm.part', 'wb').write(b'0' * 100)" $musicDl

# 300 small images for the library
& $py -c "import sys; from PIL import Image; [Image.new('RGB', (64, 80), ((i * 7) % 255, (i * 13) % 255, (i * 29) % 255)).save(f'{sys.argv[1]}/foto-{i:03d}.png') for i in range(300)]" $seed
if ($LASTEXITCODE) { throw 'could not create the test images' }
# and 3 short songs (WAV, no tools needed) for the player
& $py -c "import sys, wave, math; [(lambda w: (w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000), w.writeframes(b''.join(int(8000 * math.sin(i * (300 + 100 * n) / 8000 * 6.283)).to_bytes(2, 'little', signed=True) for i in range(8000 * 3))), w.close()))(wave.open(f'{sys.argv[1]}/tema-{n}.wav', 'wb')) for n in range(3)]" $seed
if ($LASTEXITCODE) { throw 'could not create the test songs' }
# and a long one (4 min) for the tray: a song that plays on and on, like a real one
$long = Join-Path $work 'Larga'
New-Item -ItemType Directory -Force $long | Out-Null
& $py -c "import sys, wave, array, math; a = array.array('h', (int(6000 * math.sin(i * 440 / 8000 * 6.283)) for i in range(8000 * 240))); w = wave.open(sys.argv[1] + '/larga.wav', 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(a.tobytes()); w.close()" $long
if ($LASTEXITCODE) { throw 'could not create the long test song' }

# and 600 short songs: scrolling a long music list, smooth and without stopping (C69)
$songs = Join-Path $work 'Canciones'
New-Item -ItemType Directory -Force $songs | Out-Null
& $py -c "import sys, wave; [(lambda w: (w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000), w.writeframes(b'\0\0' * 1600), w.close()))(wave.open(f'{sys.argv[1]}/cancion-{i:03d}.wav', 'wb')) for i in range(600)]" $songs
if ($LASTEXITCODE) { throw 'could not create the 600 test songs' }

# and 3500 tiny ones: the start-up budget with a big library (added late in the test)
$many = Join-Path $work 'Muchas'
New-Item -ItemType Directory -Force $many | Out-Null
& $py -c "import sys; from PIL import Image; im = Image.new('RGB', (2, 2), (90, 60, 140)); [im.save(f'{sys.argv[1]}/m-{i:04d}.png') for i in range(3500)]" $many
if ($LASTEXITCODE) { throw 'could not create the 3500 test images' }

$cfg = Join-Path $work 'smoke.conf.json'
@{
    identifier = 'dev.bakin.smoke'
    bundle     = @{ createUpdaterArtifacts = $false }
    plugins    = @{ 'deep-link' = @{ desktop = @{ schemes = @() } } }
    app        = @{ windows = @(@{
        label = 'main'; title = 'Bakin (smoke)'; width = 1280; height = 840; minWidth = 980; minHeight = 620
        center = $true; resizable = $true; decorations = $false; transparent = $false; backgroundColor = '#0d0c14'
        additionalBrowserArgs = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --enable-smooth-scrolling --remote-debugging-port=$port --remote-allow-origins=* --mute-audio"
        dataDirectory = (Join-Path $work 'wv2')
    }) }
} | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $cfg

$proc = $null
try {
    Push-Location (Join-Path $root 'desktop')
    try {
        npx tauri build --debug --no-bundle --config $cfg
        if ($LASTEXITCODE) { throw 'debug build failed' }
    } finally { Pop-Location }

    $env:BAKIN_HOME = $home_
    $env:BAKIN_MUTE = '1'  # the native player plays, silently
    $env:BAKIN_LAN_LOOPBACK = '1'  # «En tu casa» only on 127.0.0.1: no firewall prompt, nothing on the network
    # «Poner de fondo» never touches the user's desktop: requests go to a file, two fake monitors
    $env:BAKIN_DRY_WALLPAPER = '1'
    $env:BAKIN_DRY_MONITORS = '1920x1080,1080x1920'
    # «¿audio o video?» answers from a saved yt-dlp -J (a video): no network in the test
    $fake = Join-Path $work 'fake-analyze.json'
    # no BOM (Windows PowerShell's utf8 writes one; the engine reads plain UTF-8)
    [IO.File]::WriteAllText($fake, '{"title": "Video de prueba", "uploader": "Canal", "duration": 200, "extractor_key": "Generic", "categories": ["Gaming"], "formats": [{"vcodec": "avc1", "acodec": "none", "height": 1080}, {"vcodec": "none", "acodec": "opus", "abr": 130}]}')
    $env:BAKIN_FAKE_ANALYZE = $fake
    # «Buscar música» (0.15) answers from a saved catalogue: no network in the test
    $fakeYt = Join-Path $work 'fake-ytmusic.json'
    [IO.File]::WriteAllText($fakeYt, '{"search": {"songs": [{"resultType": "song", "videoId": "aaaaaaaaaaa", "title": "Tema de prueba", "artists": [{"name": "Artista de prueba"}], "album": {"name": "Disco de prueba"}, "duration_seconds": 180, "isAvailable": true}, {"resultType": "song", "videoId": "bbbbbbbbbbb", "title": "Otro tema", "artists": [{"name": "Artista de prueba"}], "duration_seconds": 200}], "albums": [{"resultType": "album", "browseId": "MPREb_prueba", "title": "Disco de prueba", "type": "Album", "year": "2026", "artists": [{"name": "Artista de prueba"}], "playlistId": "OLAK5uy_prueba"}]}, "album": {"title": "Disco de prueba", "type": "Album", "year": "2026", "artists": [{"name": "Artista de prueba"}], "audioPlaylistId": "OLAK5uy_prueba", "tracks": [{"videoId": "ccccccccccc", "title": "Pista uno", "artists": [{"name": "Artista de prueba"}]}, {"videoId": "ddddddddddd", "title": "Pista dos", "artists": [{"name": "Artista de prueba"}]}]}}')
    $env:BAKIN_FAKE_YTMUSIC = $fakeYt
    # Spotify → YouTube (0.15.2): a saved Spotify page, two songs (one the saved catalogue has)
    $fakeSp = Join-Path $work 'fake-spotify.html'
    [IO.File]::WriteAllText($fakeSp, '<script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": {"state": {"data": {"entity": {"type": "album", "name": "Lista de prueba", "trackList": [{"title": "Tema de prueba", "subtitle": "Artista de prueba", "duration": 180000}, {"title": "Algo que no existe", "subtitle": "Nadie", "duration": 90000}]}}}}}}</script>')
    $env:BAKIN_FAKE_SPOTIFY = $fakeSp
    # Wallhaven's grid from a saved page (no network): 60 results, a small and a large thumbnail each
    $fakeWh = Join-Path $work 'fake-wallhaven.json'
    & $py -c "import json, sys; s = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='; l = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mNkYPhfz4AETAxQAAAzXQIBa/IdMgAAAABJRU5ErkJggg=='; json.dump({'items': [{'id': f'fake{i:02d}', 'url': '', 'path': l, 'thumb': l, 'small': s, 'width': 1920, 'height': 1080, 'resolution': '1920x1080', 'ratio': '1.78', 'file_size': 1000, 'file_type': 'image/png', 'purity': 'sfw', 'category': 'general', 'colors': [], 'have': False} for i in range(60)], 'page': 1, 'last_page': 1, 'total': 60, 'seed': None}, open(sys.argv[1], 'w'))" $fakeWh
    $env:BAKIN_FAKE_WALLHAVEN = $fakeWh
    $env:WEBVIEW2_USER_DATA_FOLDER = Join-Path $work 'wv2'
    $env:RUST_BACKTRACE = '1'
    $proc = Start-Process (Join-Path $root 'desktop\src-tauri\target\debug\bakin-desktop.exe') -PassThru `
        -RedirectStandardOutput (Join-Path $work 'app-out.txt') -RedirectStandardError (Join-Path $work 'app-err.txt')
    $env:BAKIN_SMOKE_SONGS = $songs
    if ($Only) { $env:SMOKE_ONLY = $Only }
    node (Join-Path $PSScriptRoot 'ui-smoke.mjs') $port $seed (Join-Path $root 'desktop\src-tauri\target\debug\bakin-desktop.exe') 120 $long $home_ (Join-Path $work 'wv2') $many
    $code = $LASTEXITCODE
    if ($code) {
        # the app's own output says what went wrong (a panic, an engine error…)
        foreach ($f in 'app-err.txt', 'app-out.txt') {
            $p = Join-Path $work $f
            if ((Test-Path $p) -and (Get-Item $p).Length) {
                $lines = Get-Content $p | Group-Object | Sort-Object { $_.Group[0].ReadCount } | ForEach-Object { if ($_.Count -gt 1) { "$($_.Name)  (x$($_.Count))" } else { $_.Name } }
                Write-Host "--- $f"; $lines | Select-Object -First 40
            }
        }
        if ($proc.HasExited) { Write-Host "--- Bakin se cerró con código $($proc.ExitCode)" }
    }
} finally {
    Remove-Item Env:SMOKE_ONLY, Env:BAKIN_HOME, Env:BAKIN_MUTE, Env:BAKIN_LAN_LOOPBACK, Env:BAKIN_SMOKE_SONGS, Env:BAKIN_FAKE_ANALYZE, Env:BAKIN_FAKE_YTMUSIC, Env:BAKIN_FAKE_SPOTIFY, Env:BAKIN_FAKE_WALLHAVEN, Env:BAKIN_DRY_WALLPAPER, Env:BAKIN_DRY_MONITORS, Env:RUST_BACKTRACE, Env:WEBVIEW2_USER_DATA_FOLDER -ErrorAction SilentlyContinue
    if ($proc) { Stop-Process -Id $proc.Id -ErrorAction SilentlyContinue }
    Start-Sleep 3
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
exit $code
