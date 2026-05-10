import type { ComponentType } from 'react'
import { SetupTab } from './setup/SetupTab'
import { CharactersTab } from './characters/CharactersTab'
import { ActivitiesTab } from './activities/ActivitiesTab'
import { RulesTab } from './rules/RulesTab'
import { StatesTab } from './states/StatesTab'
import { ItemsTab } from './items/ItemsTab'
import { WorldTab } from './world/WorldTab'
import { MapTab } from './map/MapTab'
import { WorldDevTab } from './world-dev/WorldDevTab'
import { OutfitRulesTab } from './outfit-rules/OutfitRulesTab'
import { SchedulerTab } from './scheduler/SchedulerTab'

export type TabId =
  | 'setup'
  | 'characters'
  | 'activities'
  | 'rules'
  | 'states'
  | 'items'
  | 'world'
  | 'map'
  | 'world-dev'
  | 'outfit-rules'
  | 'scheduler'

export interface TabSpec {
  id: TabId
  label: string // English source — translated via t() at render time.
  Component: ComponentType
}

export const TABS: TabSpec[] = [
  { id: 'setup', label: 'Setup', Component: SetupTab },
  { id: 'characters', label: 'Characters', Component: CharactersTab },
  { id: 'activities', label: 'Activities', Component: ActivitiesTab },
  { id: 'rules', label: 'Rules', Component: RulesTab },
  { id: 'states', label: 'States', Component: StatesTab },
  { id: 'items', label: 'Items', Component: ItemsTab },
  { id: 'world', label: 'World', Component: WorldTab },
  { id: 'map', label: 'Map', Component: MapTab },
  { id: 'world-dev', label: 'World Dev', Component: WorldDevTab },
  { id: 'outfit-rules', label: 'Outfit Rules', Component: OutfitRulesTab },
  { id: 'scheduler', label: 'Scheduler', Component: SchedulerTab },
]

const TAB_IDS: ReadonlySet<string> = new Set(TABS.map((t) => t.id))

export function isTabId(value: string): value is TabId {
  return TAB_IDS.has(value)
}
