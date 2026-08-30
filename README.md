# tuxmp — terminal music player 
by ABNOX & ENJO

Search songs, play them, watch the album art render live in the
terminal. Audio via yt-dlp + VLC, UI via Rich.

## Install (one-time)

```bash
sudo apt install -y vlc
pip3 install --user --break-system-packages rich Pillow python-vlc
pip3 install --user --break-system-packages yt-dlp   # or: sudo apt install yt-dlp
```

## Run

```bash
~/bin/tuxmp
```

## Controls (simple)

| Keys    | Action                          |
|---------|---------------------------------|
| type    | search query                    |
| Enter   | search, or play highlighted song|
| 1…9     | play that song instantly        |
| ↑ / ↓   | highlight a song                |
| Space   | pause / resume                  |
| ← / →   | seek -5s / +5s                  |
| s       | stop                            |
| r       | re-search                       |
| Esc     | back to search box              |
| q       | quit (only while browsing/playing) |

## Optional: real Spotify search (results + album art from Spotify)

By default it searches YouTube — zero setup. For genuine Spotify:

1. https://developer.spotify.com/dashboard → create a free app
2. ```bash
   export SPOTIFY_CLIENT_ID="..."; export SPOTIFY_CLIENT_SECRET="..."
   ~/bin/tuxmp
   ```

Audio always streams via yt-dlp (Spotify's own streams are encrypted).
