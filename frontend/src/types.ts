import type { components } from './generated/api';
export type API = components['schemas'];
export type RawPublication = API['PublicationDTO'];
export type RawChannel = API['ChannelDTO'];
export type RawOperation = API['OperationDTO'];
export type ID = string | number;
export interface Page<T> { items: T[]; next_cursor?: string | null; total?: number; }
export interface ApiErrorData { code: string; message: string; retryable?: boolean; field_errors?: Record<string, string | string[]>; current_version?: number; details?: Record<string, unknown>; }
export interface Session { user: { id: ID; name?: string; telegram_id?: number }; csrf_token?: string; token?: string; access_token?: string; timezone?: string; }
export interface Channel extends Pick<RawChannel,'id'|'version'> {
  name: string; alias?: string; telegram_id?: number; enabled?: boolean;
  paused?: boolean; is_default?: boolean; archived?: boolean; interval_seconds?: number;
  publication_mode?: string; caption_template?: string; default_tags?: string[];
  subscriber_count?: number | null; statistics_updated_at?: string; permissions_checked_at?: string;
  permissions?: Record<string, unknown>; status?: string; queue_revision?: number;
}
export interface Asset { id: ID; source_index: number; selected: boolean; order?: number; state?: string; thumbnail_url?: string | null; filename?: string; }
export interface Publication extends Pick<RawPublication,'id'|'version'|'content_kind'|'status'|'title'|'updated_at'|'allowed_actions'> {
  title_translation?: string; description?: string; text?: string; manual_caption?: string; body?: string; caption_override?: string; assets?: Asset[];
  user_tags?: string[]; tags?: string[]; publication_mode?: string; channel_id?: ID;
  channel?: Channel | null; source?: { provider?: string; id?: ID; url?: string; author?: string; date?: string };
  album?: API['AlbumSummary'] & {items?:Asset[];initial_selected_asset_ids?:ID[];selection_limit?:number|null};
  readiness?: API['ReadinessDTO'];
  schedule?: API['ScheduleDTO'];
  error?: { code?: string; message?: string } | string | null; blocked_reason?: string;
  last_operation_id?: string | null;
  queue_movement?: {up:boolean;down:boolean}; queue_position?: number | null; scheduled_at?: string | null;
}
export interface Stage { key: string; label?: string; status: string; message?: string; }
export interface Progress { unit: 'bytes' | 'files' | 'items'; completed: number; total: number; percent?: number; estimated_seconds_remaining?: number | null; }
export interface Operation extends Pick<RawOperation,'id'|'kind'|'status'|'stage_key'|'version'|'created_at'|'updated_at'|'allowed_actions'|'last_event_seq'> {
  stages: Stage[];
  started_at?: string; heartbeat_at?: string;
  message?: string; progress?: Progress | null; result?: Record<string, unknown> | null;
  error?: { code?: string; message?: string } | string | null;
  plan?: Plan; checkpoints?: Record<string, unknown>; events?: EventRecord[];
}
export interface EventRecord { seq?: number; created_at?: string; timestamp?: string; message?: string; action?: string; status?: string; }
export interface Preview { version?: number; job_version?: number; text?: string; html?: string; caption?: string; warnings?: string[]; parts?: { text?: string; html?: string; caption_html?: string; kind?: string; source_indices?: number[]; asset_ids?: ID[]; method?: string }[]; media?: Asset[]; media_count?: number; character_count?: number; character_limit?: number; assets_prepared?:boolean; }
export interface Worker { id: ID; name?: string; machine_name?: string; provider?: string; last_seen_at?: string; status?: string; selected_model?: string; capabilities?: Record<string, unknown>; }
export interface Model { id: string; name?: string; worker_id?: ID; provider?: string; kind?: string; available?: boolean; selected?: boolean; }
export interface AITask { id: ID; status: string; kind?: string; model?: string; worker_id?: ID; operation_id?: string; created_at?: string; allowed_actions?: string[]; version?: number; }
export interface Node { node_id: string; name?: string; role: string; generation?: number; operating_system?: string; hostname?: string; profile?: string; public_url?: string; }
export interface Deployment extends Node { blocks?: Record<string, unknown> | string[]; active_sends?: number; publication_allowed?: boolean; database_path?: string; media_root?: string; data_bytes?: number; ai_location?: string; last_verified_backup?: string; migration_id?: string; moved_to?: string; }
export interface Plan { id: string; revision: number; source_node_id?: string; target_node_id?: string; estimated_bytes?: number; warning?: string; warnings?: string[]; expires_at?: string; overdue_policy?: string; skip_candidate_job_ids?:ID[]; }
export interface Backup { id: string; created_at?: string; verified_at?: string; status?: string; size_bytes?: number; operation_id?: string; }
export interface CommandResult { id?: ID; publication_id?: ID; job_id?: ID; operation_id?: string; result?: Record<string, unknown>; [key: string]: unknown; }
export interface Control { paused?: boolean; publication_allowed?: boolean; blocks?: Record<string, unknown> | ({reason:string;owner?:string}|string)[]; active_sends?: number; }
