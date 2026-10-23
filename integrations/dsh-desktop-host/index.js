import { homedir } from 'node:os'
import path from 'node:path'
import { loadHostConfig, startHost } from './host.js'

export const name = 'multinexus-dsh-desktop-host'
export const inject = ['sessionController']

export async function apply(ctx, config = {}) {
  if (config === null || typeof config !== 'object' || Array.isArray(config)
      || Object.keys(config).some(key => key !== 'enabled')
      || (config.enabled !== undefined && typeof config.enabled !== 'boolean')) {
    throw new TypeError('Desktop host unavailable.')
  }
  if (config.enabled !== true) return
  try {
    const settings = await loadHostConfig(path.join(homedir(), '.config/multinexus/dsh-desktop-host.json'))
    const host = await startHost({ ...settings, controller: ctx.sessionController })
    ctx.on('dispose', async () => {
      const status = await host.close()
      if (status.socket_replaced) ctx.logger?.warn('Desktop host socket_replaced.')
    })
  } catch {
    ctx.logger?.warn('Desktop host not ready.')
  }
}
