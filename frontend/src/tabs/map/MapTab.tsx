import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from '../../i18n/I18nProvider'
import { apiDelete, apiGet, apiPatch, apiPost } from '../../lib/api'
import { useToast } from '../../lib/Toast'

/**
 * Map tab — replaces the placement UI that used to live on the main
 * worldmap. The list-detail layout matches the other tabs: a tray on
 * the left with unplaced + passable templates, a 2D grid on the right
 * where locations are dropped onto cells.
 *
 * Drag payloads:
 *   "tmpl:<id>"  – passable template, drop on grid → POST clone at (x,y)
 *   "loc:<id>"   – existing location, drop on grid → PATCH position
 *                  (drop on tray → PATCH to (-1,-1) or DELETE if clone)
 */

interface Location {
  id: string
  name: string
  passable?: boolean
  is_template?: boolean
  template_location_id?: string
  grid_x?: number | null
  grid_y?: number | null
  map_z_offset?: number
}

const COLS = 10
const ROWS = 10
const CELL = 88

export function MapTab() {
  const { t } = useI18n()
  const { toast } = useToast()
  const [locations, setLocations] = useState<Location[] | null>(null)
  const [dragPayload, setDragPayload] = useState<string | null>(null)
  const [dragOverCell, setDragOverCell] = useState<string | null>(null)
  const [trayDragOver, setTrayDragOver] = useState(false)
  const gridRef = useRef<HTMLDivElement | null>(null)

  const reload = useCallback(async () => {
    try {
      const data = await apiGet<{ locations?: Location[] }>('/world/locations')
      setLocations(data.locations || [])
    } catch (e) {
      toast(t('Failed to load') + ': ' + (e as Error).message, 'error')
    }
  }, [t, toast])

  useEffect(() => {
    reload()
  }, [reload])

  const { placedByCell, unplaced, passableTemplates } = useMemo(() => {
    const byCell = new Map<string, Location>()
    const unp: Location[] = []
    const tmpls: Location[] = []
    for (const loc of locations || []) {
      const isClone = !!(loc.template_location_id || '').trim()
      const isPassableTemplate = !!loc.passable && !isClone
      if (isPassableTemplate) {
        tmpls.push(loc)
        continue
      }
      if (
        loc.grid_x != null &&
        loc.grid_y != null &&
        loc.grid_x >= 0 &&
        loc.grid_y >= 0
      ) {
        byCell.set(`${loc.grid_x},${loc.grid_y}`, loc)
      } else {
        unp.push(loc)
      }
    }
    return { placedByCell: byCell, unplaced: unp, passableTemplates: tmpls }
  }, [locations])

  const onDropOnCell = useCallback(
    async (gridX: number, gridY: number) => {
      const payload = dragPayload
      setDragPayload(null)
      setDragOverCell(null)
      if (!payload) return
      const cellKey = `${gridX},${gridY}`
      const occupant = placedByCell.get(cellKey)
      try {
        if (payload.startsWith('tmpl:')) {
          if (occupant) return
          const templateId = payload.slice(5)
          await apiPost(
            `/world/locations/${encodeURIComponent(templateId)}/clone`,
            { grid_x: gridX, grid_y: gridY },
          )
        } else if (payload.startsWith('loc:')) {
          const locId = payload.slice(4)
          if (occupant && occupant.id !== locId) return
          await apiPatch(
            `/world/locations/${encodeURIComponent(locId)}/position`,
            { grid_x: gridX, grid_y: gridY },
          )
        }
        await reload()
      } catch (e) {
        toast(t('Error') + ': ' + (e as Error).message, 'error')
      }
    },
    [dragPayload, placedByCell, reload, t, toast],
  )

  const onDropOnTray = useCallback(async () => {
    const payload = dragPayload
    setDragPayload(null)
    setTrayDragOver(false)
    if (!payload || !payload.startsWith('loc:')) return
    const locId = payload.slice(4)
    const loc = (locations || []).find((l) => l.id === locId)
    if (!loc) return
    const isClone = !!(loc.template_location_id || '').trim()
    try {
      if (isClone) {
        await apiDelete(`/world/locations/${encodeURIComponent(locId)}`)
      } else {
        await apiPatch(
          `/world/locations/${encodeURIComponent(locId)}/position`,
          { grid_x: -1, grid_y: -1 },
        )
      }
      await reload()
    } catch (e) {
      toast(t('Error') + ': ' + (e as Error).message, 'error')
    }
  }, [dragPayload, locations, reload, t, toast])

  const onCellDragOver = useCallback(
    (e: React.DragEvent, gridX: number, gridY: number) => {
      if (!dragPayload) return
      const cellKey = `${gridX},${gridY}`
      const occupant = placedByCell.get(cellKey)
      // Block drop if cell occupied by someone else (template clone target)
      if (dragPayload.startsWith('tmpl:') && occupant) return
      if (dragPayload.startsWith('loc:')) {
        const draggedId = dragPayload.slice(4)
        if (occupant && occupant.id !== draggedId) return
      }
      e.preventDefault()
      e.dataTransfer.dropEffect = dragPayload.startsWith('tmpl:') ? 'copy' : 'move'
      setDragOverCell(cellKey)
    },
    [dragPayload, placedByCell],
  )

  const startDragLoc = useCallback((e: React.DragEvent, loc: Location) => {
    const payload = `loc:${loc.id}`
    e.dataTransfer.setData('text/plain', payload)
    e.dataTransfer.effectAllowed = 'move'
    setDragPayload(payload)
  }, [])

  const startDragTmpl = useCallback((e: React.DragEvent, loc: Location) => {
    const payload = `tmpl:${loc.id}`
    e.dataTransfer.setData('text/plain', payload)
    e.dataTransfer.effectAllowed = 'copy'
    setDragPayload(payload)
  }, [])

  const onTrayDragOver = useCallback(
    (e: React.DragEvent) => {
      if (!dragPayload || !dragPayload.startsWith('loc:')) return
      e.preventDefault()
      e.dataTransfer.dropEffect = 'move'
      setTrayDragOver(true)
    },
    [dragPayload],
  )

  if (locations == null) {
    return <div className="ga-empty">{t('Loading…')}</div>
  }

  return (
    <div className="ga-map-layout">
      <aside
        className={'ga-map-tray' + (trayDragOver ? ' drag-over' : '')}
        onDragOver={onTrayDragOver}
        onDragLeave={() => setTrayDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          onDropOnTray()
        }}
      >
        <div className="ga-map-tray-section">
          <div className="ga-map-tray-title">{t('Unplaced')}</div>
          {unplaced.length === 0 ? (
            <div className="ga-map-tray-empty">{t('None')}</div>
          ) : (
            <div className="ga-map-tray-items">
              {unplaced.map((loc) => (
                <div
                  key={loc.id}
                  className="ga-map-tray-item"
                  draggable
                  onDragStart={(e) => startDragLoc(e, loc)}
                  onDragEnd={() => setDragPayload(null)}
                  title={loc.name}
                >
                  <img
                    className="ga-map-tray-icon"
                    src={`/world/locations/${encodeURIComponent(loc.id)}/map-icon`}
                    alt=""
                    onError={(e) => {
                      ;(e.target as HTMLImageElement).style.display = 'none'
                    }}
                  />
                  <span className="ga-map-tray-name">{loc.name}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="ga-map-tray-section">
          <div className="ga-map-tray-title">{t('Passable templates')}</div>
          {passableTemplates.length === 0 ? (
            <div className="ga-map-tray-empty">{t('None')}</div>
          ) : (
            <div className="ga-map-tray-items">
              {passableTemplates.map((loc) => (
                <div
                  key={loc.id}
                  className="ga-map-tray-item ga-map-tray-template"
                  draggable
                  onDragStart={(e) => startDragTmpl(e, loc)}
                  onDragEnd={() => setDragPayload(null)}
                  title={t('Drag onto map to place a copy')}
                >
                  <img
                    className="ga-map-tray-icon"
                    src={`/world/locations/${encodeURIComponent(loc.id)}/map-icon`}
                    alt=""
                    onError={(e) => {
                      ;(e.target as HTMLImageElement).style.display = 'none'
                    }}
                  />
                  <span className="ga-map-tray-name">{loc.name}</span>
                  <span className="ga-map-tray-stamp">∞</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="ga-map-tray-hint">
          {t('Drop a placed location here to remove it from the grid.')}
        </div>
      </aside>

      <div className="ga-map-grid-wrap" ref={gridRef}>
        <div
          className="ga-map-grid"
          style={{
            gridTemplateColumns: `repeat(${COLS}, ${CELL}px)`,
            gridTemplateRows: `repeat(${ROWS}, ${CELL}px)`,
          }}
        >
          {Array.from({ length: ROWS }).map((_, y) =>
            Array.from({ length: COLS }).map((__, x) => {
              const cellKey = `${x},${y}`
              const loc = placedByCell.get(cellKey)
              const isClone = !!(loc && (loc.template_location_id || '').trim())
              const dragOver = dragOverCell === cellKey
              const cls = [
                'ga-map-cell',
                loc ? 'occupied' : '',
                loc?.passable ? 'passable' : '',
                isClone ? 'clone' : '',
                dragOver ? 'drag-over' : '',
              ]
                .filter(Boolean)
                .join(' ')
              return (
                <div
                  key={cellKey}
                  className={cls}
                  onDragOver={(e) => onCellDragOver(e, x, y)}
                  onDragLeave={() => {
                    if (dragOverCell === cellKey) setDragOverCell(null)
                  }}
                  onDrop={(e) => {
                    e.preventDefault()
                    onDropOnCell(x, y)
                  }}
                >
                  {loc ? (
                    <div
                      className="ga-map-tile"
                      draggable
                      onDragStart={(e) => startDragLoc(e, loc)}
                      onDragEnd={() => setDragPayload(null)}
                      title={loc.name + (isClone ? ' (' + t('copy') + ')' : '')}
                    >
                      <img
                        className="ga-map-tile-bg"
                        src={`/world/locations/${encodeURIComponent(loc.id)}/map-icon`}
                        alt=""
                        onError={(e) => {
                          ;(e.target as HTMLImageElement).style.visibility =
                            'hidden'
                        }}
                      />
                      <span className="ga-map-tile-name">{loc.name}</span>
                    </div>
                  ) : (
                    <span className="ga-map-cell-coord">
                      {x},{y}
                    </span>
                  )}
                </div>
              )
            }),
          )}
        </div>
      </div>
    </div>
  )
}
