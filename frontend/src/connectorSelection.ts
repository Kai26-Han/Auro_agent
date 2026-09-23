import type { ConnectorSummary } from './ConnectorCatalog';

// Keep in sync with connector_limits.py. The limit bounds model tool schemas,
// not connector rows; never silently select only part of a connector.
export const MAX_CONNECTOR_TOOLS = 100;

export function connectorSelections(connectors: ConnectorSummary[], value: string[], allowed?: string[]) {
  const selected = new Set(value);
  const grants = allowed === undefined ? undefined : new Set(allowed);
  return connectors.map(connector => ({
    connector,
    selectedIds: connector.tools.filter(tool => selected.has(tool.id)).map(tool => tool.id),
    eligibleIds: connector.tools.filter(tool => tool.policy !== 'disabled' && (!grants || grants.has(tool.id))).map(tool => tool.id),
  }));
}

export function missingConnectorTools(connectors: ConnectorSummary[], value: string[]) {
  const known = new Set(connectors.flatMap(connector => connector.tools.map(tool => tool.id)));
  return value.filter(id => !known.has(id));
}

export function toggleConnectorSelection(row: ReturnType<typeof connectorSelections>[number], value: string[]) {
  // Allow removal even after disconnect, revocation, or permission changes.
  if (row.selectedIds.length) {
    const ids = new Set(row.selectedIds);
    return value.filter(id => !ids.has(id));
  }
  if (row.connector.status !== 'connected' || !row.eligibleIds.length) return value;
  const next = [...new Set([...value, ...row.eligibleIds])];
  if (next.length > MAX_CONNECTOR_TOOLS) throw new Error('connector_tool_limit');
  return next;
}
