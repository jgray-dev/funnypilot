"""Bounded, uncompressed snapshots of drive files, including active recordings.

Run iteration and close on the web server's background worker. Each file's size
is fixed after opening: following a growing file to EOF would corrupt the tar
headers and could make an on-road download never finish.
"""
import os
import tarfile

CHUNK_SIZE = 256 * 1024


def tar_chunks(route, segments, filenames, segment_path):
  for segment in segments:
    directory = segment_path(route, segment)
    if directory is None:
      continue
    for filename in filenames:
      try:
        source = open(os.path.join(directory, filename), 'rb')
      except FileNotFoundError:
        # Retention can remove a segment before we open it. Once opened, Unix
        # keeps its bytes available even if retention subsequently unlinks it.
        continue
      with source:
        stat = os.fstat(source.fileno())
        header = tarfile.TarInfo(f'{route}--{segment}/{filename}')
        header.size = stat.st_size
        header.mtime = int(stat.st_mtime)
        yield header.tobuf()
        remaining = stat.st_size
        while remaining:
          chunk = source.read(min(CHUNK_SIZE, remaining))
          if not chunk:
            raise OSError(f'recording truncated during download: {filename}')
          remaining -= len(chunk)
          yield chunk
        padding = (-stat.st_size) % 512
        if padding:
          yield b'\0' * padding
  yield b'\0' * 1024
