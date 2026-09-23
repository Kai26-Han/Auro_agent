import {useEffect,useState} from 'react';
import {api} from './api';
// Keep a newly built UI compatible with the running server until restart.
export function useMem0Procedures(){
 const [enabled,setEnabled]=useState(false);
 useEffect(()=>{let active=true;api<{channels:{procedure?:{enabled:boolean}}}>('/memory/mem0/capabilities').then(value=>{if(active)setEnabled(value.channels.procedure?.enabled===true)}).catch(()=>{});return()=>{active=false}},[]);
 return enabled;
}
