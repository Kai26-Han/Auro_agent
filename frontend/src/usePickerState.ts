import { useCallback, useState } from 'react';

export type PickerPresentation = { embedded?:boolean; onDismiss?:()=>void };

/** Embedded pickers share the composer's single popup and its close/focus rules. */
export function usePickerState(embedded=false,onDismiss?:()=>void) {
  const [open,setLocalOpen]=useState(embedded);
  const setOpen=useCallback((next:boolean)=>{setLocalOpen(next);if(!next&&embedded)onDismiss?.();},[embedded,onDismiss]);
  return [open,setOpen] as const;
}
