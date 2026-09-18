export interface BattenSummary {
  batten_id: string;
  capacity_grams: number;
  total_grams: number;
  remaining_grams: number;
  load_count?: number;
}

export interface LoadItem {
  load_id: number;
  piece_id: string;
  weight_grams: number;
  created_at: string | null;
}

export interface BattenDetail extends BattenSummary {
  loads: LoadItem[];
}

export interface LoadResult {
  accepted: boolean;
  reason?: string;
  message: string;
  batten_id?: string;
  capacity_grams?: number;
  total_grams?: number;
  remaining_grams?: number;
}

export interface TransferResult {
  accepted: boolean;
  reason?: string;
  message: string;
  load_id?: number;
  piece_id?: string;
  weight_grams?: number;
  source_batten_id?: string;
  target_batten_id?: string;
  source?: BattenSummary;
  target?: BattenSummary;
}

export interface CorrectWeightResult {
  accepted: boolean;
  reason?: string;
  message: string;
  batten_id?: string;
  load_id?: number;
  piece_id?: string;
  previous_weight_grams?: number;
  weight_grams?: number;
  capacity_grams?: number;
  total_grams?: number;
  remaining_grams?: number;
}
