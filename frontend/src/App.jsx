import { useEffect, useRef, useState } from "react";

const API = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
const FINAL_JOB_STATUSES = new Set(["completed", "failed"]);
const DEFAULT_BROWSER_OPTIONS = [
  { value: "none", label: "Sem cookies" },
  { value: "safari", label: "Safari" },
  { value: "chrome", label: "Chrome" },
  { value: "firefox", label: "Firefox" },
  { value: "brave", label: "Brave" },
];

function buildApiUrl(path) {
  return `${API}${path}`;
}

function formatBytes(bytes) {
  if (!bytes) return null;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(seconds) {
  if (!seconds) return null;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function getErrorMessage(res, fallback) {
  const contentType = res.headers.get("content-type") || "";

  if (contentType.includes("application/json")) {
    try {
      const data = await res.json();
      return data.detail || data.message || fallback;
    } catch {
      return fallback;
    }
  }

  const text = await res.text();
  return text.trim() || fallback;
}

function getPreferredFormat(formats, maxHeight) {
  return (formats || []).find((format) => format.height && format.height <= maxHeight) || null;
}

function buildPresetOptions(info) {
  if (!info) return [];

  const videoFormats = (info.formats || []).filter((format) => format.height);
  const presets = [
    {
      key: "best",
      label: "Melhor MP4",
      description: "Qualidade maxima",
      formatId: "bestvideo+bestaudio/best",
      audioOnly: false,
    },
    getPreferredFormat(videoFormats, 1080) && {
      key: "1080",
      label: "1080p",
      description: "Alta qualidade",
      formatId: getPreferredFormat(videoFormats, 1080).format_id,
      audioOnly: false,
    },
    getPreferredFormat(videoFormats, 720) && {
      key: "720",
      label: "720p",
      description: "Arquivo mais leve",
      formatId: getPreferredFormat(videoFormats, 720).format_id,
      audioOnly: false,
    },
    {
      key: "mp3",
      label: "MP3",
      description: "Somente audio",
      formatId: "bestaudio/best",
      audioOnly: true,
    },
  ].filter(Boolean);

  return presets.filter(
    (preset, index) =>
      presets.findIndex(
        (current) =>
          current.formatId === preset.formatId && current.audioOnly === preset.audioOnly,
      ) === index,
  );
}

export default function App() {
  const [url, setUrl] = useState("");
  const [browser, setBrowser] = useState("none");
  const [browserOptions, setBrowserOptions] = useState(DEFAULT_BROWSER_OPTIONS);
  const [info, setInfo] = useState(null);
  const [loading, setLoading] = useState(false);
  const [downloadJob, setDownloadJob] = useState(null);
  const [error, setError] = useState(null);

  const eventSourceRef = useRef(null);
  const iframeRef = useRef(null);
  const triggeredJobIdRef = useRef("");

  useEffect(() => {
    let ignore = false;

    async function loadConfig() {
      try {
        const res = await fetch(buildApiUrl("/config"));
        if (!res.ok) return;

        const data = await res.json();
        if (ignore) return;

        if (Array.isArray(data.browser_options) && data.browser_options.length > 0) {
          setBrowserOptions(data.browser_options);
        }
        if (data.default_browser) {
          setBrowser(data.default_browser);
        }
      } catch {
        if (ignore) return;
      }
    }

    loadConfig();

    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    if (!downloadJob?.id || FINAL_JOB_STATUSES.has(downloadJob.status)) return undefined;

    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    const source = new EventSource(buildApiUrl(`/download-jobs/${downloadJob.id}/events`));
    eventSourceRef.current = source;

    source.onmessage = (event) => {
      if (!event.data) return;

      try {
        const nextJob = JSON.parse(event.data);
        setDownloadJob(nextJob);

        if (nextJob.status === "failed" && nextJob.error) {
          setError(nextJob.error);
        }

        if (
          nextJob.status === "completed"
          && nextJob.file_url
          && triggeredJobIdRef.current !== nextJob.id
        ) {
          triggeredJobIdRef.current = nextJob.id;
          triggerBrowserDownload(nextJob.file_url, iframeRef);
        }
      } catch {
        setError("Nao foi possivel acompanhar o progresso do download.");
      }
    };

    source.onerror = () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
    };

    return () => {
      source.close();
      if (eventSourceRef.current === source) {
        eventSourceRef.current = null;
      }
    };
  }, [downloadJob?.id, downloadJob?.status]);

  useEffect(() => {
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
      if (iframeRef.current) {
        iframeRef.current.remove();
      }
    };
  }, []);

  async function handleFetchInfo(event) {
    event.preventDefault();
    if (!url.trim()) return;

    setLoading(true);
    setError(null);
    setInfo(null);
    setDownloadJob(null);

    try {
      const params = new URLSearchParams({
        url: url.trim(),
        browser,
      });
      const res = await fetch(buildApiUrl(`/info?${params}`));
      if (!res.ok) throw new Error(await getErrorMessage(res, "Erro ao buscar opcoes"));

      const data = await res.json();
      setInfo(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleDownload(formatId, audioOnly = false) {
    setError(null);
    triggeredJobIdRef.current = "";

    try {
      const res = await fetch(buildApiUrl("/download-jobs"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          url: url.trim(),
          format_id: formatId,
          audio_only: audioOnly,
          browser,
        }),
      });

      if (!res.ok) throw new Error(await getErrorMessage(res, "Erro no download"));
      const job = await res.json();
      setDownloadJob(job);
    } catch (err) {
      setError(err.message);
    }
  }

  const presets = buildPresetOptions(info);
  const activeDownloadKey = downloadJob
    ? `${downloadJob.audio_only ? "mp3" : downloadJob.format_id}`
    : null;
  const isDownloadActive = downloadJob && !FINAL_JOB_STATUSES.has(downloadJob.status);

  return (
    <main className="app-shell">
      <section className="panel">
        <div className="hero">
          <h1>Baixar video</h1>
          <p>YouTube ou Instagram. Cole o link, carregue as opcoes e baixe.</p>
        </div>

        <form className="search-form" onSubmit={handleFetchInfo}>
          <input
            type="url"
            className="url-input"
            placeholder="https://www.youtube.com/... ou https://www.instagram.com/..."
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            required
          />
          <div className="toolbar">
            <select
              className="browser-select"
              value={browser}
              onChange={(event) => setBrowser(event.target.value)}
              title="Autenticacao"
            >
              {browserOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? <span className="spinner" /> : "Carregar"}
            </button>
          </div>
        </form>

        {error && (
          <div className="message message-error">
            <strong>Erro:</strong> {error}
          </div>
        )}

        {downloadJob && (
          <div className="message message-progress">
            <div className="progress-head">
              <strong>{downloadJob.stage || "Processando"}</strong>
              <span>{downloadJob.progress ? `${downloadJob.progress.toFixed(1)}%` : "Na fila"}</span>
            </div>
            <div className="progress-track">
              <div
                className="progress-fill"
                style={{ width: `${Math.max(downloadJob.progress || 4, 4)}%` }}
              />
            </div>
            <div className="progress-meta">
              <span>{downloadJob.speed || "Aguardando velocidade"}</span>
              <span>{downloadJob.eta ? `ETA ${downloadJob.eta}` : "Sem ETA"}</span>
            </div>
            {downloadJob.status === "completed" && downloadJob.file_url && (
              <div className="download-link">
                Se o download nao iniciar sozinho,{" "}
                <a href={buildApiUrl(downloadJob.file_url)}>clique aqui</a>.
              </div>
            )}
          </div>
        )}

        {info && (
          <div className="result">
            <div className="media-head">
              {info.thumbnail && (
                <img src={info.thumbnail} alt="Thumbnail" className="thumbnail" />
              )}
              <div className="media-copy">
                <span className="platform-tag">{info.platform_label || "Video"}</span>
                <h2>{info.title}</h2>
                <div className="media-meta">
                  {info.uploader && <span>{info.uploader}</span>}
                  {info.duration && <span>{formatDuration(info.duration)}</span>}
                </div>
              </div>
            </div>

            {presets.length > 0 && (
              <div className="option-group">
                <h3>Opcoes rapidas</h3>
                <div className="option-grid">
                  {presets.map((preset) => (
                    <button
                      key={preset.key}
                      type="button"
                      className="option-card"
                      onClick={() => handleDownload(preset.formatId, preset.audioOnly)}
                      disabled={isDownloadActive}
                    >
                      <strong>{preset.label}</strong>
                      <span>{preset.description}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="option-group">
              <h3>Formatos de video</h3>
              <div className="format-list">
                {(info.formats || []).map((format) => {
                  const key = `${format.format_id}`;
                  const isCurrent = activeDownloadKey === key;

                  return (
                    <div key={format.format_id} className="format-row">
                      <div className="format-copy">
                        <strong>{format.label}</strong>
                        {format.filesize && <span>{formatBytes(format.filesize)}</span>}
                      </div>
                      <button
                        type="button"
                        className="btn btn-download"
                        onClick={() => handleDownload(format.format_id)}
                        disabled={isDownloadActive}
                      >
                        {isCurrent && isDownloadActive ? (
                          <><span className="spinner" /> Baixando</>
                        ) : (
                          "Baixar MP4"
                        )}
                      </button>
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="option-group">
              <h3>Somente audio</h3>
              <div className="format-row">
                <div className="format-copy">
                  <strong>MP3</strong>
                  <span>Alta qualidade</span>
                </div>
                <button
                  type="button"
                  className="btn btn-download btn-audio"
                  onClick={() => handleDownload("bestaudio/best", true)}
                  disabled={isDownloadActive}
                >
                  {activeDownloadKey === "mp3" && isDownloadActive ? (
                    <><span className="spinner" /> Baixando</>
                  ) : (
                    "Baixar MP3"
                  )}
                </button>
              </div>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}

function triggerBrowserDownload(fileUrl, iframeRef) {
  let iframe = iframeRef.current;

  if (!iframe) {
    iframe = document.createElement("iframe");
    iframe.style.display = "none";
    document.body.appendChild(iframe);
    iframeRef.current = iframe;
  }

  const absoluteUrl = buildApiUrl(fileUrl);
  const separator = absoluteUrl.includes("?") ? "&" : "?";
  iframe.src = `${absoluteUrl}${separator}ts=${Date.now()}`;
}
