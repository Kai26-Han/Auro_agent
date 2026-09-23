import llamaLogo from './assets/engine-logos/llamaindex.png';
import pageLogo from './assets/engine-logos/pageindex.svg';

/** Official marks bundled by Vite; labels beside each icon provide its name. */
export function EngineLogo({ engine, size = 24 }: { engine: 'llamaindex' | 'pageindex'; size?: number }) {
  return <img className="engine-logo" src={engine === 'pageindex' ? pageLogo : llamaLogo} alt="" aria-hidden="true" width={size} height={size} draggable={false}/>;
}
