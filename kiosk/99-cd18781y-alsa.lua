-- ThinkSmart View CD-18781Y: force TAS playback to S16LE 48 kHz.
-- Wrong node match leaves the sink at s24-32le, which sounds nearly dead.
-- Load as 99- so this wins over WirePlumber's built-in ALSA defaults.
alsa_monitor.rules = alsa_monitor.rules or {}

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "equals", "alsa_card.platform-c051000.sound-card" },
    },
  },
  apply_properties = {
    ["api.alsa.use-acp"] = true,
    ["api.alsa.use-ucm"] = true,
    ["api.acp.auto-profile"] = true,
    ["api.acp.auto-port"] = true,
    ["api.alsa.disable-mmap"] = true,
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.HiFi__Speaker__sink" } },
    { { "node.name", "equals", "alsa_output.platform-c051000.sound-card.HiFi__hw_cd18781y_0__sink" } },
    { { "node.name", "matches", "alsa_output.platform-c051000.sound-card.HiFi__*Speaker*" } },
  },
  apply_properties = {
    ["node.pause-on-idle"] = false,
    ["session.suspend-timeout-seconds"] = 0,
    ["audio.format"] = "S16LE",
    ["audio.rate"] = 48000,
    ["audio.channels"] = 2,
    ["api.alsa.headroom"] = 8192,
  },
})
