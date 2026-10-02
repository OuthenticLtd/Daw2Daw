-- Probe a REAPER plug-in's saved state: for every parameter, its name and
-- displayed value at a few settings, and which bytes of the plug-in's own
-- data block change when it moves. Run headless by tools/reaper_probe.py:
--   reaper.exe -nosplash -new reaper_probe.lua  (reads probe_in.txt beside it)
local dir = ({reaper.get_action_context()})[2]:match("^(.*[\\/])")
local f = io.open(dir .. "probe_in.txt", "r")
local fxname = f:read("*l")
local outpath = f:read("*l")
f:close()
local out = io.open(outpath, "w")
reaper.InsertTrackAtIndex(0, false)
local tr = reaper.GetTrack(0, 0)
local fx = reaper.TrackFX_AddByName(tr, fxname, false, -1)
out:write("FX\t", fxname, "\t", tostring(fx), "\n")
local function chunk()
  local _, c = reaper.GetTrackStateChunk(tr, "", false)
  return c
end
local n = reaper.TrackFX_GetNumParams(tr, fx)
out:write("N\t", n, "\n")
out:write("BASE\t", chunk():gsub("\n", "\\n"), "\n")
for i = 0, n - 1 do
  local _, pn = reaper.TrackFX_GetParamName(tr, fx, i, "")
  local v0 = reaper.TrackFX_GetParamNormalized(tr, fx, i)
  local line = { tostring(i), pn, string.format("%.6f", v0) }
  for _, v in ipairs({0.0, 0.25, 0.5, 0.75, 1.0}) do
    reaper.TrackFX_SetParamNormalized(tr, fx, i, v)
    local _, fv = reaper.TrackFX_GetFormattedParamValue(tr, fx, i, "")
    line[#line + 1] = string.format("%.2f=%s", v, fv)
  end
  reaper.TrackFX_SetParamNormalized(tr, fx, i, 0.3)
  out:write("P\t", table.concat(line, "\t"), "\n")
  out:write("C\t", tostring(i), "\t", chunk():gsub("\n", "\\n"), "\n")
  reaper.TrackFX_SetParamNormalized(tr, fx, i, v0)
end
out:write("END\n")
out:close()
