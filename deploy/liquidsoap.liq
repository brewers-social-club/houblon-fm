set("log.stdout", true)
set("server.telnet", true)
fallback_file = "/data/fallback.mp3"
def next_track() =
  try
    result = http.get(timeout=5., "http://sidecar:8081/next")
    if result.status_code == 200 and string.trim(result) != "" then
      string.trim(result)
    else
      fallback_file
    end
  catch err do
    log.warn("Programmateur indisponible: #{err.message}")
    fallback_file
  end
end
radio = request.dynamic(id="scheduler", fun () -> request.create(next_track()))
radio = mksafe(fallback(track_sensitive=false, [radio, single(fallible=true, fallback_file)]))
output.icecast(%mp3(bitrate=128), host="icecast", port=8000, password=environment.get("ICECAST_SOURCE_PASSWORD"), mount="live.mp3", name="Houblon FM", description="Radio brassicole écrite et lue par des IA", radio)
