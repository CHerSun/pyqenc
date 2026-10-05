# Samples

- Video samples are not going to be included with the project. Use your own videos.
- Samples include metrics and their plot from 2 approaches to encoding for comparison.
  - `full_encoding` sub-folder - stores data from full-file encoding with constant quality.
  - `pyqenc` sub-folder - stores data from `pyqenc` encoding of the same video.

## Building the e2e sample (`sample-lion-fullhd.mkv`)

The e2e suites (`tests/e2e/`) skip without a sample at this exact path. It is
a small MKV exercising every stream kind — build it from any real content:

```sh
mkdir samples\_build
ffmpeg -y -ss 1200 -t 35 -i <real_video.mkv> -map 0:v:0 -c copy samples\_build\sample_video.mkv
ffmpeg -y -f lavfi -i "sine=frequency=440:sample_rate=48000:duration=35" -c:a aac -b:a 128k samples\_build\sample_aac.m4a
ffmpeg -y -f lavfi -i "sine=frequency=880:sample_rate=48000:duration=35" -c:a flac samples\_build\sample_flac.flac
mkvextract <real_video.mkv> attachments 1:samples\_build\cover.jpg
# plus a 3-line samples\_build\sample_subs.srt and samples\_build\sample_chapters.xml (3 chapters)
mkvmerge -q -o samples\sample-lion-fullhd.mkv --title "Sample Lion FullHD (e2e)" \
  --chapters samples\_build\sample_chapters.xml samples\_build\sample_video.mkv \
  --language 0:eng --track-name 0:"Surround AAC" samples\_build\sample_aac.m4a \
  --language 0:rus --track-name 0:"Stereo FLAC" samples\_build\sample_flac.flac \
  --language 0:eng --track-name 0:"Full subs" samples\_build\sample_subs.srt \
  --attachment-name cover.jpg --attachment-mime-type image/jpeg --attach-file samples\_build\cover.jpg
```

`samples/_build/` is gitignored (intermediate build inputs).

