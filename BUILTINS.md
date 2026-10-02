# Built-in effects across Cubase, REAPER and Ableton Live

A DAW's own effects load nowhere else, so each becomes the closest of the
other DAW's own effects, settings carried over, never printed. REAPER's own
(Cockos plug-ins and the JS effects every REAPER ships) are the hub: Cubase
maps to them and back (`stock.py`, `freq_eq.py`, `natives.py`), Live maps to
them and back (`live_stock.py`), so every pair of DAWs is covered.

How close a mapping is, as the conversion log says it:

- **exact / close**: the same processing with the same settings (within a
  fraction of a dB where measured)
- **measured**: fitted to renders of the effect itself (Cubase 15, Live 11,
  REAPER 7 on noise; `tools/cubase_fx_testbed.py`, `tools/fx_response.py`)
- **approximate**: the same kind of effect with its main settings; its
  character is another algorithm's

`tools/natives_check.py` round-trips every mapping below (106 cases).

## EQ and filters

| Cubase | REAPER | Live | |
|---|---|---|---|
| Channel EQ | ReaEQ | EQ Eight | measured |
| Frequency (8 bands) | ReaEQ | EQ Eight | measured, within 0.2 dB |
| StudioEQ | ReaEQ | EQ Eight | measured, within 0.2 dB |
| DJ-Eq (kills too) | ReaEQ | EQ Eight | measured, within 0.4 dB (kills 1.4) |
| GEQ-10 / GEQ-30 | ReaEQ | EQ Eight | measured, within 1.5 dB |
| EQ-M5 / EQ-P1A | ReaEQ | EQ Eight | measured, within 0.7 dB |
| - | JS RBJ high/low pass, 4- and 7-band | EQ Eight | exact |
| - | JS LOSER 3/4-band | EQ Eight | close |
| WahWah | JS Wah-Wah | Auto Filter (band pass) | approximate |
| - | - | Auto Filter (static) | approximate |

## Dynamics

| Cubase | REAPER | Live | |
|---|---|---|---|
| Compressor | ReaComp | Compressor | measured |
| Limiter / Brickwall Limiter | ReaLimit | Limiter | close |
| Gate | ReaGate | Gate | close |
| Expander | JS Downward Expander | Compressor (Expand) | close (Live: ratio to 1:2) |
| DeEsser | ReaComp (detector band) | Compressor (sidechain EQ) | close |
| Maximizer / Raiser | ReaLimit | Limiter | approximate / close |
| VoxComp, Black Valve, Tube, Vintage | ReaComp | Compressor | approximate |
| VSTDynamics | ReaGate + ReaComp + ReaLimit | Gate + Compressor + Limiter | close |
| MultibandCompressor / Expander | ReaXcomp | Multiband Dynamics | close (Live: 3 bands) |
| Squasher | ReaXcomp | Multiband Dynamics | approximate |
| - | ReaComp | Glue Compressor (to REAPER) | close |
| EnvelopeShaper | JS Transient Controller | Drum Buss (transients) | approximate |

## Delay, reverb, modulation, pitch

| Cubase | REAPER | Live | |
|---|---|---|---|
| StereoDelay / PingPongDelay | ReaDelay | Delay | measured |
| MonoDelay, StudioDelay, ModMachine, MultiTap | ReaDelay | Delay | close / approximate |
| - | JS delays (plain, tone, tempo, ping-pong) | Delay | close |
| - | ReaDelay | Echo, Filter Delay, Grain Delay (to REAPER) | close / approximate |
| RoomWorks, REVelation, REVerence, Shimmer | ReaVerbate | Reverb, Hybrid Reverb | measured / approximate |
| Chorus, StudioChorus, Cloner | JS Chorus | Chorus-Ensemble | approximate |
| Vibrato | JS Chorus (all wet) | Chorus-Ensemble (Vibrato) | close |
| Flanger / Phaser | JS Flanger / Phaser | Phaser-Flanger | approximate |
| Tremolo / AutoPan | JS Tremolo / Ping Pong Pan | Auto Pan | close |
| Rotary | JS Tremolo + Chorus | Auto Pan + Chorus-Ensemble | approximate |
| Octaver / PitchShifter | ReaPitch | Shifter (pitch) | close |
| StereoEnhancer, MonoToStereo, Imager, Volume | JS width / channel mixer / volume | Utility | exact / close |

## Distortion

| Cubase | REAPER | Live | |
|---|---|---|---|
| Distortion, Distroyer, Quadrafuzz, AmpSimulator, VST Amp Rack, Bass Amp | JS Distortion | Overdrive / Pedal / Amp | approximate |
| Magneto II | JS Saturation | Saturator / Dynamic Tube / Vinyl | approximate |
| SoftClipper | JS Soft Clipper | Saturator (soft clip) | close |
| - | JS bit reduction | Redux (bit depth) | close |
| - | ReaEQ (speaker band) | Cabinet | approximate |

## Not carried (nothing like them elsewhere)

Left out with a note in the log saying so and to render the track in its own
DAW first:

- **Cubase:** Vocoder, VocalChain, Pitch Correct, FX Modulator, and its
  legacy VST2-era effects (Bitcrusher, Chopper, DaTube, Grungelizer,
  Metalizer, RingModulator, StepFilter, Tranceformer), AutoFilter,
  DualFilter, MorphFilter and ToneBooster (the last four are being measured).
- **Live:** Corpus, Resonators, Vocoder, Beat Repeat, Looper, Spectral
  Resonator, Spectral Time, Erosion, Max for Live devices.
- Meters and tuners (SuperVision, Tuner, Spectrum) make no sound and are
  left out.
