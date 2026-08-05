const configuredBaseURL = import.meta.env.VITE_API_BASE_URL?.trim();
const baseURL = configuredBaseURL || (import.meta.env.DEV ? "http://localhost:8000" : "");

export interface ResearchRequest {
  topic: string;
  search_api?: string;
  job_id: string;
}

export interface ResearchStreamEvent {
  type: string;
  [key: string]: unknown;
}

export interface StreamOptions {
  signal?: AbortSignal;
}

async function responseError(response: Response): Promise<string> {
  const fallback = `研究请求失败，状态码：${response.status}`;
  const text = await response.text().catch(() => "");
  try {
    const payload = JSON.parse(text) as { detail?: unknown };
    return typeof payload.detail === "string" && payload.detail.trim()
      ? payload.detail.trim()
      : fallback;
  } catch {
    return text || fallback;
  }
}

export async function runResearchStream(
  payload: ResearchRequest,
  onEvent: (event: ResearchStreamEvent) => void,
  options: StreamOptions = {}
): Promise<void> {
  const response = await fetch(`${baseURL}/research/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream"
    },
    body: JSON.stringify(payload),
    signal: options.signal
  });

  if (!response.ok) {
    throw new Error(await responseError(response));
  }

  const body = response.body;
  if (!body) {
    throw new Error("浏览器不支持流式响应，无法获取研究进度");
  }

  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const rawEvent = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 2);

      if (rawEvent.startsWith("data:")) {
        const dataPayload = rawEvent.slice(5).trim();
        if (dataPayload) {
          try {
            const event = JSON.parse(dataPayload) as ResearchStreamEvent;
            onEvent(event);

            if (event.type === "error" || event.type === "cancelled" || event.type === "done") {
              return;
            }
          } catch (error) {
            console.error("解析流式事件失败：", error, dataPayload);
          }
        }
      }

      boundary = buffer.indexOf("\n\n");
    }

    if (done) {
      // 处理可能的尾巴事件
      if (buffer.trim()) {
        const rawEvent = buffer.trim();
        if (rawEvent.startsWith("data:")) {
          const dataPayload = rawEvent.slice(5).trim();
          if (dataPayload) {
            try {
              const event = JSON.parse(dataPayload) as ResearchStreamEvent;
              onEvent(event);
            } catch (error) {
              console.error("解析流式事件失败：", error, dataPayload);
            }
          }
        }
      }
      break;
    }
  }
}

export async function cancelResearchJob(jobId: string): Promise<void> {
  const response = await fetch(`${baseURL}/research/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST"
  });
  if (!response.ok && response.status !== 404) {
    throw new Error(await responseError(response));
  }
}
