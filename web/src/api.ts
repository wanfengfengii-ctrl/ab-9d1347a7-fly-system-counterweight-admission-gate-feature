import type {
  BattenDetail,
  BattenSummary,
  CorrectionResult,
  LoadResult,
  TransferResult,
} from './types';

// 浏览器内为空串（相对路径，经 vite 代理）；Vitest 下为真实 API 地址
const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? '';

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<{ status: number; body: T }> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  const body = (await res.json()) as T;
  return { status: res.status, body };
}

export function fetchBattens() {
  return request<{ battens: BattenSummary[] }>('/api/battens');
}

export function fetchBatten(battenId: string) {
  return request<BattenDetail>(`/api/battens/${encodeURIComponent(battenId)}`);
}

export function submitLoad(battenId: string, pieceId: string, weightGrams: number) {
  return request<LoadResult>(`/api/battens/${encodeURIComponent(battenId)}/loads`, {
    method: 'POST',
    body: JSON.stringify({ piece_id: pieceId, weight_grams: weightGrams }),
  });
}

export function transferLoad(
  sourceBattenId: string,
  loadId: number,
  targetBattenId: string,
) {
  return request<TransferResult>(
    `/api/battens/${encodeURIComponent(sourceBattenId)}/loads/${loadId}/transfer`,
    {
      method: 'POST',
      body: JSON.stringify({ target_batten_id: targetBattenId }),
    },
  );
}

export function correctLoadWeight(
  battenId: string,
  loadId: number,
  weightGrams: number,
) {
  return request<CorrectionResult>(
    `/api/battens/${encodeURIComponent(battenId)}/loads/${loadId}/correct-weight`,
    {
      method: 'POST',
      body: JSON.stringify({ weight_grams: weightGrams }),
    },
  );
}
