set("log.stdout", true)
set("server.telnet", true)
fallback_file = "/data/fallback.mp3"
def next_track() =
  result = process.read("curl -fsS http://sidecar:8081/next")
  if result == "" then fallback_file else string.trim(result) end
end
radio = request.dynamic(id="scheduler", fun () -> request.create(next_track()))
radio = mksafe(fallback(track_sensitive=false, [radio, single(fallback_file)]))
output.icecast(%mp3(bitrate=128), host="icecast", port=8000, password=environment.get("ICECAST_SOURCE_PASSWORD"), mount="live.mp3", name="Houblon FM", description="Radio brassicole écrite et lue par des IA", radio)
