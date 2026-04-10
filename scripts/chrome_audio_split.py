#!/usr/bin/env python3
"""Control a dedicated Chrome instance and route tabs to audio output devices."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from math import cos, pi, sin
from dataclasses import dataclass
from itertools import count

from websocket import create_connection


DEFAULT_WATCH_URLS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=djV11Xbc914",
]


@dataclass
class Target:
    id: str
    title: str
    url: str
    type: str
    websocket_debugger_url: str


class ChromeCDP:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self._connections: dict[str, object] = {}
        self._request_ids = count(1)

    def _get_json(self, path: str) -> object:
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def _put_json(self, path: str) -> object:
        request = urllib.request.Request(self.base + path, method="PUT")
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def list_targets(self) -> list[Target]:
        raw_targets = self._get_json("/json/list")
        return [
            Target(
                id=raw["id"],
                title=raw.get("title", ""),
                url=raw.get("url", ""),
                type=raw.get("type", ""),
                websocket_debugger_url=raw.get("webSocketDebuggerUrl", ""),
            )
            for raw in raw_targets
        ]

    def list_pages(self) -> list[Target]:
        return [target for target in self.list_targets() if target.type == "page"]

    def close_target(self, target_id: str) -> None:
        urllib.request.urlopen(self.base + "/json/close/" + target_id)

    def new_tab(self, url: str) -> Target:
        raw = self._put_json("/json/new?" + urllib.parse.quote(url, safe=""))
        return Target(
            id=raw["id"],
            title=raw.get("title", ""),
            url=raw.get("url", ""),
            type=raw.get("type", ""),
            websocket_debugger_url=raw.get("webSocketDebuggerUrl", ""),
        )

    def close(self) -> None:
        for ws in list(self._connections.values()):
            try:
                ws.close()
            except Exception:
                pass
        self._connections.clear()

    def _connection_for(self, target: Target):
        ws = self._connections.get(target.id)
        if ws is not None:
            return ws
        ws = create_connection(target.websocket_debugger_url)
        self._connections[target.id] = ws
        return ws

    def eval(self, target: Target, expression: str) -> object:
        request_id = next(self._request_ids)
        ws = self._connection_for(target)
        try:
            ws.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": expression,
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    }
                )
            )

            while True:
                message = json.loads(ws.recv())
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(message["error"])
                result = message.get("result", {})
                if result.get("exceptionDetails"):
                    raise RuntimeError(result["exceptionDetails"])
                return result.get("result", {}).get("value")
        except Exception:
            try:
                ws.close()
            except Exception:
                pass
            self._connections.pop(target.id, None)
            raise

    def __del__(self) -> None:
        self.close()


def outputs_script() -> str:
    return r"""
(async () => {
  const stream = await navigator.mediaDevices.getUserMedia({audio:true});
  stream.getTracks().forEach(track => track.stop());
  return (await navigator.mediaDevices.enumerateDevices())
    .filter(device => device.kind === 'audiooutput')
    .map(device => ({ label: device.label, deviceId: device.deviceId }));
})()
""".strip()


def persistent_audio_control_script(
    *,
    sink_label: str | None = None,
    rate: float | None = None,
    volume: float | None = None,
    muted: bool | None = None,
    play_after: bool = False,
) -> str:
    sink_expr = "null" if sink_label is None else json.dumps(sink_label)
    rate_expr = "null" if rate is None else json.dumps(rate)
    volume_expr = "null" if volume is None else json.dumps(volume)
    muted_expr = "null" if muted is None else ("true" if muted else "false")
    play_after_expr = "true" if play_after else "false"
    return f"""
(async () => {{
  const updates = {{
    sinkLabel: {sink_expr},
    rate: {rate_expr},
    volume: {volume_expr},
    muted: {muted_expr},
    playAfter: {play_after_expr},
  }};
  const state = window.__codexAudioControlState || {{
    sinkLabel: null,
    sinkId: null,
    rate: null,
    volume: null,
    muted: null,
    observerInstalled: false,
  }};
  Object.assign(state, Object.fromEntries(Object.entries(updates).filter(([, v]) => v !== null)));

  const shouldRefreshSink = updates.sinkLabel !== null || !state.sinkId;
  if (state.sinkLabel && shouldRefreshSink) {{
    const stream = await navigator.mediaDevices.getUserMedia({{audio:true}});
    stream.getTracks().forEach(track => track.stop());
    const outputs = (await navigator.mediaDevices.enumerateDevices())
      .filter(device => device.kind === 'audiooutput')
      .map(device => ({{ label: device.label, deviceId: device.deviceId }}));
    const output =
      outputs.find(device => device.deviceId !== 'default' && device.label === state.sinkLabel) ||
      outputs.find(device => device.deviceId !== 'default' && device.label.includes(state.sinkLabel)) ||
      outputs.find(device => device.label === state.sinkLabel || device.label.includes(state.sinkLabel));
    if (!output) {{
      return {{ ok: false, error: 'device-not-found', desiredLabel: state.sinkLabel, outputs }};
    }}
    state.sinkId = output.deviceId;
    state.outputs = outputs;
  }}

  async function apply(element) {{
    if (!element) return;
    if (state.sinkId && typeof element.setSinkId === 'function' && element.sinkId !== state.sinkId) {{
      try {{
        await element.setSinkId(state.sinkId);
      }} catch (error) {{
        console.warn('setSinkId failed', error);
      }}
    }}
    if (state.rate !== null) {{
      element.preservesPitch = true;
      element.playbackRate = state.rate;
    }}
    if (state.volume !== null) {{
      element.volume = Math.max(0, Math.min(1, state.volume));
    }}
    if (state.muted !== null) {{
      element.muted = state.muted;
    }}
    if (state.playAfter && element.paused) {{
      try {{
        await element.play();
      }} catch (error) {{
        console.warn('play failed', error);
      }}
    }}
  }}

  async function applyAll() {{
    const elements = [...document.querySelectorAll('audio,video')];
    await Promise.all(elements.map(apply));
    return elements;
  }}

  window.__codexAudioControlState = state;
  window.__codexApplyAll = applyAll;

  if (!state.observerInstalled) {{
    const observer = new MutationObserver(records => {{
      for (const record of records) {{
        for (const node of record.addedNodes) {{
          if (node.nodeType !== Node.ELEMENT_NODE) continue;
          if (node.matches?.('audio,video')) apply(node);
          node.querySelectorAll?.('audio,video').forEach(apply);
        }}
      }}
      window.__codexApplyAll?.().catch(error => console.warn('applyAll failed', error));
    }});
    observer.observe(document.documentElement, {{ childList: true, subtree: true }});
    window.__codexSinkObserver = observer;
    window.__codexSinkInterval = setInterval(() => {{
      window.__codexApplyAll?.().catch(error => console.warn('applyAll failed', error));
    }}, 3000);
    state.observerInstalled = true;
  }}

  const elements = await applyAll();
  return {{
    ok: true,
    desiredLabel: state.sinkLabel,
    sinkId: state.sinkId,
    mediaCount: elements.length,
    sinkIds: elements.map(element => element.sinkId),
    playbackRate: elements.map(element => element.playbackRate),
    volume: elements.map(element => element.volume),
    muted: elements.map(element => element.muted),
    outputs: state.outputs || [],
  }};
}})()
""".strip()


def set_sink_script(label: str, play: bool) -> str:
    script = persistent_audio_control_script(sink_label=label, play_after=play)
    marker = "return {"
    replacement = (
        "window.__codexDesiredSink = { label: window.__codexAudioControlState.sinkLabel, "
        "sinkId: window.__codexAudioControlState.sinkId };\n  return {"
    )
    return script.replace(marker, replacement, 1)


def page_status_script() -> str:
    return r"""
(() => {
  const elements = [...document.querySelectorAll('audio,video')];
  const state = window.__codexAudioControlState || null;
  const desired =
    state && (state.sinkLabel || state.sinkId)
      ? { label: state.sinkLabel || null, sinkId: state.sinkId || null }
      : (window.__codexDesiredSink ?? null);
  const playerBarTitle = document.querySelector('ytmusic-player-bar .title')?.textContent?.trim() || null;
  const playerBarByline = [...document.querySelectorAll('ytmusic-player-bar .byline a, ytmusic-player-bar .byline yt-formatted-string')]
    .map(node => node.textContent?.trim())
    .filter(Boolean)
    .join(' • ') || null;
  const repeatHost = document.querySelector('ytmusic-player-bar yt-icon-button.repeat, ytmusic-player-bar #expand-repeat');
  const shuffleHost = document.querySelector('ytmusic-player-bar yt-icon-button.shuffle, ytmusic-player-bar #expand-shuffle');
  const repeatLabel =
    repeatHost?.getAttribute('title') ||
    repeatHost?.getAttribute('label') ||
    repeatHost?.querySelector('button')?.getAttribute('aria-label') ||
    null;
  const shuffleLabel =
    shuffleHost?.getAttribute('title') ||
    shuffleHost?.getAttribute('label') ||
    shuffleHost?.querySelector('button')?.getAttribute('aria-label') ||
    null;
  return {
    title: document.title,
    url: location.href,
    playerBarTitle,
    playerBarByline,
    mediaCount: elements.length,
    sinkIds: elements.map(element => element.sinkId),
    currentTime: elements.map(element => element.currentTime),
    duration: elements.map(element => element.duration),
    paused: elements.map(element => element.paused),
    playbackRate: elements.map(element => element.playbackRate),
    volume: elements.map(element => element.volume),
    muted: elements.map(element => element.muted),
    repeatLabel,
    shuffleLabel,
    desired,
  };
})()
""".strip()


def queue_lock_script(*, repeat_off: bool = True, shuffle_off: bool = True) -> str:
    repeat_expr = "true" if repeat_off else "false"
    shuffle_expr = "true" if shuffle_off else "false"
    return f"""
(async () => {{
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function describe(host) {{
    if (!host) return null;
    return (
      host.getAttribute('title') ||
      host.getAttribute('label') ||
      host.querySelector('button')?.getAttribute('aria-label') ||
      null
    );
  }}
  async function forceState(selector, predicate) {{
    const host = document.querySelector(selector);
    if (!host) return {{ ok: false, error: 'not-found', selector }};
    const button = host.querySelector('button') || host;
    let label = describe(host);
    for (let attempt = 0; attempt < 4; attempt++) {{
      if (predicate(label)) {{
        return {{ ok: true, label, attempts: attempt }};
      }}
      button.click();
      await sleep(120);
      label = describe(host);
    }}
    return {{ ok: predicate(label), label, attempts: 4 }};
  }}

  const result = {{}};
  if ({repeat_expr}) {{
    result.repeat = await forceState(
      'ytmusic-player-bar yt-icon-button.repeat, ytmusic-player-bar #expand-repeat',
      label => !!label && /repeat off/i.test(label),
    );
  }}
  if ({shuffle_expr}) {{
    result.shuffle = await forceState(
      'ytmusic-player-bar yt-icon-button.shuffle, ytmusic-player-bar #expand-shuffle',
      label => !!label && /shuffle$/i.test(label),
    );
  }}
  return result;
}})()
""".strip()


def set_rate_script(rate: float) -> str:
    return persistent_audio_control_script(rate=rate)


def set_volume_script(volume: float, muted: bool | None = None) -> str:
    return persistent_audio_control_script(volume=volume, muted=muted)


def analyze_media_script(seconds: float, interval_ms: int = 50) -> str:
    return f"""
(async () => {{
  const seconds = {json.dumps(seconds)};
  const intervalMs = {json.dumps(interval_ms)};
  const element = document.querySelector('audio,video');
  if (!element) return {{ ok: false, error: 'no-media-element' }};
  const stream = element.captureStream ? element.captureStream() : (element.mozCaptureStream ? element.mozCaptureStream() : null);
  if (!stream) return {{ ok: false, error: 'capture-stream-unavailable' }};

  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0.1;
  source.connect(analyser);

  const timeData = new Float32Array(analyser.fftSize);
  const freqData = new Uint8Array(analyser.frequencyBinCount);
  const rms = [];
  const peaks = [];
  const zeroCrossings = [];
  const centroids = [];
  const rolloffs = [];
  const flux = [];
  const subEnergy = [];
  const bassEnergy = [];
  const lowMidEnergy = [];
  const vocalEnergy = [];
  const presenceEnergy = [];
  const highEnergy = [];
  let prevFreq = null;
  const bins = analyser.frequencyBinCount;
  const binHz = (ctx.sampleRate / 2) / bins;

  function bandSum(minHz, maxHz) {{
    const startBin = Math.max(0, Math.floor(minHz / binHz));
    const endBin = Math.min(freqData.length - 1, Math.ceil(maxHz / binHz));
    let total = 0;
    for (let i = startBin; i <= endBin; i++) total += freqData[i];
    return total;
  }}

  const start = performance.now();
  while ((performance.now() - start) / 1000 < seconds) {{
    analyser.getFloatTimeDomainData(timeData);
    analyser.getByteFrequencyData(freqData);
    let sumSq = 0;
    let peak = 0;
    let zeroCross = 0;
    for (let i = 0; i < timeData.length; i++) sumSq += timeData[i] * timeData[i];
    for (let i = 0; i < timeData.length; i++) {{
      const value = Math.abs(timeData[i]);
      if (value > peak) peak = value;
      if (i > 0) {{
        const a = timeData[i - 1];
        const b = timeData[i];
        if ((a >= 0 && b < 0) || (a < 0 && b >= 0)) zeroCross += 1;
      }}
    }}
    rms.push(Math.sqrt(sumSq / timeData.length));
    peaks.push(peak);
    zeroCrossings.push(zeroCross / Math.max(1, timeData.length - 1));

    let weighted = 0;
    let total = 0;
    for (let i = 0; i < freqData.length; i++) {{
      weighted += freqData[i] * i * binHz;
      total += freqData[i];
    }}
    centroids.push(total > 0 ? weighted / total : 0);
    if (total > 0) {{
      let cumulative = 0;
      let rolloff = 0;
      for (let i = 0; i < freqData.length; i++) {{
        cumulative += freqData[i];
        if (cumulative >= total * 0.85) {{
          rolloff = i * binHz;
          break;
        }}
      }}
      rolloffs.push(rolloff);
    }} else {{
      rolloffs.push(0);
    }}

    const safeTotal = Math.max(1, total);
    subEnergy.push(bandSum(20, 60) / safeTotal);
    bassEnergy.push(bandSum(60, 180) / safeTotal);
    lowMidEnergy.push(bandSum(180, 600) / safeTotal);
    vocalEnergy.push(bandSum(300, 3400) / safeTotal);
    presenceEnergy.push(bandSum(2000, 4500) / safeTotal);
    highEnergy.push(bandSum(4500, 12000) / safeTotal);

    if (prevFreq) {{
      let diff = 0;
      for (let i = 0; i < freqData.length; i++) {{
        const delta = freqData[i] - prevFreq[i];
        if (delta > 0) diff += delta;
      }}
      flux.push(diff);
    }}
    prevFreq = Array.from(freqData);
    await new Promise(resolve => setTimeout(resolve, intervalMs));
  }}

  function percentile(values, p) {{
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.max(0, Math.floor((sorted.length - 1) * p)));
    return sorted[idx];
  }}

  function estimateBpm(values) {{
    if (values.length < 16) return null;
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    const centered = values.map(v => Math.max(0, v - mean));
    const sampleRate = 1000 / intervalMs;
    const minBpm = 80;
    const maxBpm = 170;
    const minLag = Math.floor(sampleRate * 60 / maxBpm);
    const maxLag = Math.ceil(sampleRate * 60 / minBpm);
    let bestLag = null;
    let bestScore = -Infinity;
    let secondScore = -Infinity;
    for (let lag = minLag; lag <= maxLag; lag++) {{
      let score = 0;
      for (let i = lag; i < centered.length; i++) score += centered[i] * centered[i - lag];
      if (score > bestScore) {{
        secondScore = bestScore;
        bestScore = score;
        bestLag = lag;
      }} else if (score > secondScore) {{
        secondScore = score;
      }}
    }}
    if (!bestLag || bestScore <= 0) return null;
    return {{
      bpm: (60 * sampleRate) / bestLag,
      confidence: secondScore > 0 ? bestScore / secondScore : bestScore,
    }};
  }}

  const bpm = estimateBpm(flux.length ? flux : rms);
  const avgRms = rms.length ? rms.reduce((a, b) => a + b, 0) / rms.length : 0;
  const avgPeak = peaks.length ? peaks.reduce((a, b) => a + b, 0) / peaks.length : 0;
  const crestFactor = avgRms > 1e-6 ? avgPeak / avgRms : 0;
  const avgCentroid = centroids.length ? centroids.reduce((a, b) => a + b, 0) / centroids.length : 0;
  const avgRolloff = rolloffs.length ? rolloffs.reduce((a, b) => a + b, 0) / rolloffs.length : 0;
  const fluxMean = flux.length ? flux.reduce((a, b) => a + b, 0) / flux.length : 0;
  const dynamicRange = percentile(rms, 0.95) - percentile(rms, 0.2);
  const transientDensity = flux.length ? flux.filter(v => v > fluxMean * 1.4).length / flux.length : 0;

  function avg(values) {{
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
  }}

  const result = {{
    ok: true,
    title: document.title,
    currentTime: element.currentTime,
    seconds,
    avgRms,
    rmsP95: percentile(rms, 0.95),
    rmsP20: percentile(rms, 0.2),
    avgPeak,
    crestFactor,
    avgZeroCrossingRate: avg(zeroCrossings),
    avgCentroidHz: avgCentroid,
    avgRolloffHz: avgRolloff,
    fluxMean,
    transientDensity,
    dynamicRange,
    subEnergyRatio: avg(subEnergy),
    bassEnergyRatio: avg(bassEnergy),
    lowMidEnergyRatio: avg(lowMidEnergy),
    vocalEnergyRatio: avg(vocalEnergy),
    presenceEnergyRatio: avg(presenceEnergy),
    highEnergyRatio: avg(highEnergy),
    bpm,
  }};
  source.disconnect();
  analyser.disconnect();
  stream.getTracks().forEach(track => track.stop());
  await ctx.close();
  return result;
}})()
""".strip()


def playlist_tracks_script() -> str:
    return r"""
(() => {
  const anchors = [...document.querySelectorAll('ytmusic-responsive-list-item-renderer a[href*="watch?v="]')];
  const seen = new Set();
  const tracks = [];
  for (const anchor of anchors) {
    const href = anchor.href || '';
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const row = anchor.closest('ytmusic-responsive-list-item-renderer');
    const title = anchor.textContent?.trim() || '';
    const byline = row?.querySelectorAll?.('.secondary-flex-columns yt-formatted-string, .secondary-flex-columns a, .byline a, .byline yt-formatted-string') || [];
    const subtitle = [...byline].map(node => node.textContent?.trim()).filter(Boolean).join(' • ');
    tracks.push({ index: tracks.length, title, subtitle, url: href });
  }
  return { ok: true, count: tracks.length, tracks };
})()
""".strip()


def choose_track_script(*, title_substring: str | None = None, index: int | None = None) -> str:
    title_expr = "null" if title_substring is None else json.dumps(title_substring.lower())
    index_expr = "null" if index is None else str(index)
    return f"""
(() => {{
  const desiredTitle = {title_expr};
  const desiredIndex = {index_expr};
  const anchors = [...document.querySelectorAll('ytmusic-responsive-list-item-renderer a[href*="watch?v="]')];
  const seen = new Set();
  const tracks = [];
  for (const anchor of anchors) {{
    const href = anchor.href || '';
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const row = anchor.closest('ytmusic-responsive-list-item-renderer');
    const title = anchor.textContent?.trim() || '';
    const byline = row?.querySelectorAll?.('.secondary-flex-columns yt-formatted-string, .secondary-flex-columns a, .byline a, .byline yt-formatted-string') || [];
    const subtitle = [...byline].map(node => node.textContent?.trim()).filter(Boolean).join(' • ');
    tracks.push({{ index: tracks.length, title, subtitle, url: href, anchor }});
  }}
  let selected = null;
  if (desiredIndex !== null) {{
    selected = tracks.find(track => track.index === desiredIndex) || null;
  }} else if (desiredTitle !== null) {{
    selected = tracks.find(track => track.title.toLowerCase().includes(desiredTitle)) || null;
  }}
  if (!selected) {{
    return {{
      ok: false,
      error: 'track-not-found',
      available: tracks.map(track => ({{
        index: track.index,
        title: track.title,
        subtitle: track.subtitle,
        url: track.url,
      }})),
    }};
  }}
  selected.anchor.click();
  return {{
    ok: true,
    selected: {{
      index: selected.index,
      title: selected.title,
      subtitle: selected.subtitle,
      url: selected.url,
    }},
  }};
}})()
""".strip()


def transport_script(*, action: str, seconds: float | None = None) -> str:
    seconds_expr = "null" if seconds is None else json.dumps(seconds)
    return f"""
(async () => {{
  const element = document.querySelector('audio,video');
  if (!element) return {{ ok: false, error: 'no-media-element' }};
  const action = {json.dumps(action)};
  const seconds = {seconds_expr};
  const nextButton = document.querySelector('ytmusic-player-bar tp-yt-paper-icon-button.next-button');
  const prevButton = document.querySelector('ytmusic-player-bar tp-yt-paper-icon-button.previous-button');
  const playButton = document.querySelector('ytmusic-player-bar tp-yt-paper-icon-button.play-pause-button');
  if (action === 'play') {{
    try {{
      await element.play();
    }} catch (error) {{
      playButton?.click();
    }}
    if (element.paused) playButton?.click();
  }}
  else if (action === 'pause') element.pause();
  else if (action === 'next' && nextButton) nextButton.click();
  else if (action === 'prev' && prevButton) prevButton.click();
  else if (action === 'seek-abs' && seconds !== null) {{
    const target = Math.max(0, Math.min(Number.isFinite(element.duration) ? element.duration : seconds, seconds));
    element.currentTime = target;
  }} else if (action === 'seek-rel' && seconds !== null) {{
    const target = element.currentTime + seconds;
    const clamped = Math.max(0, Math.min(Number.isFinite(element.duration) ? element.duration : target, target));
    element.currentTime = clamped;
  }} else {{
    return {{ ok: false, error: 'unsupported-action', action }};
  }}
  return {{
    ok: true,
    action,
    title: document.title,
    currentTime: element.currentTime,
    duration: element.duration,
    paused: element.paused,
  }};
}})()
""".strip()


def select_music_pages(cdp: ChromeCDP) -> list[Target]:
    return [
        page
        for page in cdp.list_pages()
        if page.url.startswith("https://music.youtube.com/")
        or page.url.startswith("https://www.youtube.com/watch")
    ]


def find_page(cdp: ChromeCDP, *, title_substring: str | None, index: int | None) -> Target:
    pages = select_music_pages(cdp)
    if title_substring is not None:
        lowered = title_substring.lower()
        for page in pages:
            if lowered in page.title.lower():
                return page
        raise SystemExit(f"No music tab matched title substring: {title_substring!r}")
    if index is not None:
        if index < 0 or index >= len(pages):
            raise SystemExit(f"Music tab index out of range: {index}")
        return pages[index]
    raise SystemExit("Either --title or --index is required")


def find_page_by_sink_label(cdp: ChromeCDP, sink_label: str) -> Target:
    lowered = sink_label.lower()
    for page in select_music_pages(cdp):
        status = cdp.eval(page, page_status_script())
        desired = status.get("desired") if isinstance(status, dict) else None
        if isinstance(desired, dict):
            label = str(desired.get("label", ""))
            if lowered in label.lower():
                return page
    raise SystemExit(f"No music tab matched sink label: {sink_label!r}")


def command_setup_ytm(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)

    for page in cdp.list_pages():
        if page.url.startswith("chrome://intro"):
            cdp.close_target(page.id)

    urls = args.urls or DEFAULT_WATCH_URLS
    for url in urls:
        cdp.new_tab(url)

    time.sleep(args.wait_seconds)
    pages = select_music_pages(cdp)
    selected_pages = pages[-len(args.devices) :]

    if len(selected_pages) < len(args.devices):
        raise SystemExit(f"Expected {len(args.devices)} music.youtube.com tabs, found {len(selected_pages)}")

    result: dict[str, object] = {"tabs": []}
    for page, device in zip(selected_pages, args.devices, strict=True):
        tab_info = {
            "title": page.title,
            "url": page.url,
            "outputs": cdp.eval(page, outputs_script()),
            "route": cdp.eval(page, set_sink_script(device, play=args.play)),
            "status": cdp.eval(page, page_status_script()),
        }
        result["tabs"].append(tab_info)

    print(json.dumps(result, indent=2))


def command_status(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    pages = select_music_pages(cdp)
    result = []
    for page in pages:
        result.append(cdp.eval(page, page_status_script()))
    print(json.dumps(result, indent=2))


def command_route_tab(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page = find_page(cdp, title_substring=args.title, index=args.index)
    result = {
        "title": page.title,
        "url": page.url,
        "route": cdp.eval(page, set_sink_script(args.device, play=args.play)),
        "status": cdp.eval(page, page_status_script()),
    }
    print(json.dumps(result, indent=2))


def command_set_rate(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page = find_page(cdp, title_substring=args.title, index=args.index)
    result = {
        "title": page.title,
        "url": page.url,
        "rate": cdp.eval(page, set_rate_script(args.rate)),
        "status": cdp.eval(page, page_status_script()),
    }
    print(json.dumps(result, indent=2))


def command_set_volume(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page = find_page(cdp, title_substring=args.title, index=args.index)
    muted = None
    if args.mute:
        muted = True
    elif args.unmute:
        muted = False
    result = {
        "title": page.title,
        "url": page.url,
        "volume": cdp.eval(page, set_volume_script(args.volume, muted=muted)),
        "status": cdp.eval(page, page_status_script()),
    }
    print(json.dumps(result, indent=2))


def command_analyze_tab(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page = find_page(cdp, title_substring=args.title, index=args.index)
    result = {
        "title": page.title,
        "url": page.url,
        "analysis": cdp.eval(page, analyze_media_script(args.seconds, interval_ms=args.interval_ms)),
        "status": cdp.eval(page, page_status_script()),
    }
    print(json.dumps(result, indent=2))


def command_crossfade(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page_from = find_page(cdp, title_substring=args.from_title, index=args.from_index)
    page_to = find_page(cdp, title_substring=args.to_title, index=args.to_index)

    steps = max(1, int(args.steps))
    duration = max(0.1, float(args.seconds))
    for step in range(steps + 1):
        progress = step / steps
        if args.shape == "equal-power":
            out_vol = cos(progress * (pi / 2.0))
            in_vol = sin(progress * (pi / 2.0))
        else:
            out_vol = 1.0 - progress
            in_vol = progress
        cdp.eval(page_from, set_volume_script(out_vol, muted=False))
        cdp.eval(page_to, set_volume_script(in_vol, muted=False))
        if step < steps:
            time.sleep(duration / steps)

    result = {
        "from": {
            "title": page_from.title,
            "status": cdp.eval(page_from, page_status_script()),
        },
        "to": {
            "title": page_to.title,
            "status": cdp.eval(page_to, page_status_script()),
        },
    }
    print(json.dumps(result, indent=2))


def staged_mix_levels(progress: float) -> tuple[float, float]:
    progress = max(0.0, min(1.0, progress))
    if progress < 0.25:
        local = progress / 0.25
        out_vol = 1.0
        in_vol = 0.0 + 0.28 * local
    elif progress < 0.65:
        local = (progress - 0.25) / 0.40
        out_vol = 1.0 - 0.38 * local
        in_vol = 0.28 + 0.47 * local
    else:
        local = (progress - 0.65) / 0.35
        out_vol = 0.62 * (1.0 - local)
        in_vol = 0.75 + 0.25 * local
    return max(0.0, min(1.0, out_vol)), max(0.0, min(1.0, in_vol))


def command_staged_mix(args: argparse.Namespace) -> None:
    cdp = ChromeCDP(args.port)
    page_from = find_page(cdp, title_substring=args.from_title, index=args.from_index)
    page_to = find_page(cdp, title_substring=args.to_title, index=args.to_index)

    steps = max(1, int(args.steps))
    duration = max(0.1, float(args.seconds))
    for step in range(steps + 1):
        progress = step / steps
        out_vol, in_vol = staged_mix_levels(progress)
        cdp.eval(page_from, set_volume_script(out_vol, muted=False))
        cdp.eval(page_to, set_volume_script(in_vol, muted=False))
        if step < steps:
            time.sleep(duration / steps)

    result = {
        "from": {
            "title": page_from.title,
            "status": cdp.eval(page_from, page_status_script()),
        },
        "to": {
            "title": page_to.title,
            "status": cdp.eval(page_to, page_status_script()),
        },
    }
    print(json.dumps(result, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup-ytm", help="Open music.youtube.com tabs and route each to a device")
    setup.add_argument("--port", type=int, default=9333)
    setup.add_argument("--wait-seconds", type=float, default=5.0)
    setup.add_argument("--play", action="store_true")
    setup.add_argument(
        "--device",
        dest="devices",
        action="append",
        required=True,
        help="Audio output label substring to assign to the next YTM tab",
    )
    setup.add_argument(
        "--url",
        dest="urls",
        action="append",
        help="Optional YTM watch URL. Defaults to two known public tracks.",
    )
    setup.set_defaults(func=command_setup_ytm)

    status = subparsers.add_parser("status", help="Show sink status for open music.youtube.com tabs")
    status.add_argument("--port", type=int, default=9333)
    status.set_defaults(func=command_status)

    route = subparsers.add_parser("route-tab", help="Route one existing music tab to a device")
    route.add_argument("--port", type=int, default=9333)
    route.add_argument("--title", help="Case-insensitive title substring matcher")
    route.add_argument("--index", type=int, help="Zero-based music tab index from `status`")
    route.add_argument("--device", required=True, help="Audio output label substring to assign")
    route.add_argument("--play", action="store_true")
    route.set_defaults(func=command_route_tab)

    rate = subparsers.add_parser("set-rate", help="Set playback rate on one existing music tab")
    rate.add_argument("--port", type=int, default=9333)
    rate.add_argument("--title", help="Case-insensitive title substring matcher")
    rate.add_argument("--index", type=int, help="Zero-based music tab index from `status`")
    rate.add_argument("--rate", type=float, required=True)
    rate.set_defaults(func=command_set_rate)

    analyze = subparsers.add_parser("analyze-tab", help="Analyze one existing music tab")
    analyze.add_argument("--port", type=int, default=9333)
    analyze.add_argument("--title", help="Case-insensitive title substring matcher")
    analyze.add_argument("--index", type=int, help="Zero-based music tab index from `status`")
    analyze.add_argument("--seconds", type=float, default=6.0)
    analyze.add_argument("--interval-ms", type=int, default=50)
    analyze.set_defaults(func=command_analyze_tab)

    volume = subparsers.add_parser("set-volume", help="Set volume on one existing music tab")
    volume.add_argument("--port", type=int, default=9333)
    volume.add_argument("--title", help="Case-insensitive title substring matcher")
    volume.add_argument("--index", type=int, help="Zero-based music tab index from `status`")
    volume.add_argument("--volume", type=float, required=True)
    volume.add_argument("--mute", action="store_true")
    volume.add_argument("--unmute", action="store_true")
    volume.set_defaults(func=command_set_volume)

    crossfade = subparsers.add_parser("crossfade", help="Crossfade between two existing music tabs")
    crossfade.add_argument("--port", type=int, default=9333)
    crossfade.add_argument("--from-title", help="Case-insensitive title substring for outgoing tab")
    crossfade.add_argument("--from-index", type=int, help="Outgoing tab index")
    crossfade.add_argument("--to-title", help="Case-insensitive title substring for incoming tab")
    crossfade.add_argument("--to-index", type=int, help="Incoming tab index")
    crossfade.add_argument("--seconds", type=float, default=8.0)
    crossfade.add_argument("--steps", type=int, default=32)
    crossfade.add_argument("--shape", choices=["equal-power", "linear"], default="equal-power")
    crossfade.set_defaults(func=command_crossfade)

    staged = subparsers.add_parser(
        "staged-mix",
        help="DJ-style staged transition: introduce next track, hold overlap, then hand off",
    )
    staged.add_argument("--port", type=int, default=9333)
    staged.add_argument("--from-title", help="Case-insensitive title substring for outgoing tab")
    staged.add_argument("--from-index", type=int, help="Outgoing tab index")
    staged.add_argument("--to-title", help="Case-insensitive title substring for incoming tab")
    staged.add_argument("--to-index", type=int, help="Incoming tab index")
    staged.add_argument("--seconds", type=float, default=12.0)
    staged.add_argument("--steps", type=int, default=48)
    staged.set_defaults(func=command_staged_mix)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
