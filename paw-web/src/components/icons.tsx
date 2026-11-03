import type { ReactNode } from 'react'

function Svg({ size = 18, children }: { size?: number; children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {children}
    </svg>
  )
}
type P = { size?: number }
export const IconUpload = (p: P) => <Svg {...p}><path d="M12 16V4m0 0l-4 4m4-4l4 4" /><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3" /></Svg>
export const IconSearch = (p: P) => <Svg {...p}><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></Svg>
export const IconFile = (p: P) => <Svg {...p}><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z" /><path d="M14 3v5h5" /></Svg>
export const IconX = (p: P) => <Svg {...p}><path d="M6 6l12 12M18 6L6 18" /></Svg>
export const IconCheck = (p: P) => <Svg {...p}><path d="M5 12l5 5 9-10" /></Svg>
export const IconAlert = (p: P) => <Svg {...p}><path d="M12 3l10 18H2z" /><path d="M12 10v5m0 3h.01" /></Svg>
export const IconDownload = (p: P) => <Svg {...p}><path d="M12 4v12m0 0l-4-4m4 4l4-4" /><path d="M4 20h16" /></Svg>
export const IconArrowLeft = (p: P) => <Svg {...p}><path d="M19 12H5m0 0l6-6m-6 6l6 6" /></Svg>
export const IconArrowRight = (p: P) => <Svg {...p}><path d="M5 12h14m0 0l-6-6m6 6l-6 6" /></Svg>
export const IconSliders = (p: P) => <Svg {...p}><path d="M4 7h10m4 0h2M4 17h2m4 0h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></Svg>
export const IconSpark = (p: P) => <Svg {...p}><path d="M12 3l2 6 6 2-6 2-2 6-2-6-6-2 6-2z" /></Svg>
export const IconLogo = (p: P) => <Svg {...p}><circle cx="7" cy="9" r="2" /><circle cx="12" cy="6" r="2" /><circle cx="17" cy="9" r="2" /><path d="M7 17c0-3 2.2-5 5-5s5 2 5 5c0 1.5-1.2 2.5-2.700 2.200L12 18.700l-2.300.5C8.200 19.500 7 18.500 7 17z" /></Svg>
export const IconChat = (p: P) => <Svg {...p}><path d="M21 12a8 8 0 01-11.700 7L4 20l1.200-4.300A8 8 0 1121 12z" /></Svg>
export const IconStop = (p: P) => <Svg {...p}><rect x="6" y="6" width="12" height="12" rx="2" /></Svg>
export const IconSend = (p: P) => <Svg {...p}><path d="M4 12l16-8-6 16-2-7z" /></Svg>
export const IconInfo = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 11v5m0-8h.01" /></Svg>
export const IconPlug = (p: P) => <Svg {...p}><path d="M9 3v5m6-5v5" /><path d="M6 8h12v3a6 6 0 01-12 0z" /><path d="M12 17v4" /></Svg>
