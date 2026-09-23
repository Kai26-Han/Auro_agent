import { Cpu } from 'lucide-react';
import anthropic from './assets/provider-logos/anthropic.svg';
import deepseek from './assets/provider-logos/deepseek.svg';

const officialLogos: Record<string, string> = {
  anthropic,
  deepseek,
};

/** Official local brand assets; adjacent text supplies the accessible name. */
export function ProviderLogo({ provider, size = 16 }: { provider?: string; size?: number }) {
  const logo = provider ? officialLogos[provider] : undefined;
  return logo
    ? <img className="provider-logo" src={logo} alt="" aria-hidden="true" width={size} height={size} draggable={false}/>
    : <Cpu size={size}/>;
}
