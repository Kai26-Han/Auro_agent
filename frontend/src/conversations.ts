export type SessionSummary = {
  id:string; title:string; status:string; mode:string; updated:string;
  project_id?:string|null; pinned?:boolean; pinned_at?:string|null;
  archived?:boolean; archived_at?:string|null;
};

export type Project = {
  id:string; name:string; sort_order:number; conversation_count:number;
  pinned:boolean; pinned_at?:string|null; archived:boolean; archived_at?:string|null;
  created:string; updated:string;
};

export type SessionPage = {
  items:SessionSummary[]; total:number; offset:number; limit:number; has_more:boolean;
};
