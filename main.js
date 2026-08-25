const { app, BrowserWindow, ipcMain, screen, session, systemPreferences } = require('electron')
const path = require('path')

const MIN_WIDTH = 300
const MIN_HEIGHT = 460

let win
let captureWin
let captureLoaded = Promise.resolve()
let dragInterval = null
let dragOffset = null
let collapsed = false
let ignoringMouse = false
let micOp = Promise.resolve()
let micReq = 0
let capturing = false

function describeOverlay() {
  if (!win || win.isDestroyed()) return { destroyed: true }
  return {
    visible: win.isVisible(),
    minimized: win.isMinimized(),
    opacity: win.getOpacity(),
    bounds: win.getBounds(),
    alwaysOnTop: win.isAlwaysOnTop()
  }
}

function ensureOverlayVisible(reason) {
  if (!win || win.isDestroyed()) return
  win.setAlwaysOnTop(true)
  if (win.isMinimized()) win.restore()
  if (!win.isVisible()) {
    console.log('[michelle] overlay hidden; showing', reason, describeOverlay())
    win.show()
  }
}

function loadCaptureWindow() {
  if (captureWin && !captureWin.isDestroyed()) return captureLoaded
  captureWin = new BrowserWindow({
    show: false,
    width: 8,
    height: 8,
    frame: false,
    skipTaskbar: true,
    transparent: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
      backgroundThrottling: false
    }
  })
  captureWin.setMenuBarVisibility(false)
  captureWin.webContents.setAudioMuted(true)
  captureLoaded = captureWin.loadFile(path.join(__dirname, 'mic-capture.html'))
  captureWin.on('closed', () => {
    captureWin = null
    captureLoaded = Promise.resolve()
  })
  return captureLoaded
}

function createWindow() {
  win = new BrowserWindow({
    width: 380,
    height: 600,
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    frame: false,
    alwaysOnTop: true,
    transparent: true,
    resizable: true,
    fullscreenable: false,
    hasShadow: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
      backgroundThrottling: false
    }
  })
  win.loadFile('index.html')
  win.setAlwaysOnTop(true)

  win.on('hide', () => {
    console.log('[michelle] overlay hide', describeOverlay())
    if (capturing) ensureOverlayVisible('hide-during-mic')
  })

  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(permission === 'media' || permission === 'microphone')
  })

  loadCaptureWindow().catch((err) => {
    console.log('[michelle] mic capture window failed to load', err && err.message)
  })
}

function setIgnore(ignore) {
  ignoringMouse = !!ignore
  if (ignore) win.setIgnoreMouseEvents(true, { forward: true })
  else win.setIgnoreMouseEvents(false)
}

// Collapsed = click-through window with the orb still in place (no resize flash).
ipcMain.on('collapse', (event, isCollapsed) => {
  collapsed = !!isCollapsed
  if (collapsed) setIgnore(true)
  else setIgnore(false)
  event.returnValue = true
})

ipcMain.on('set-ignore-mouse', (event, ignore) => {
  if (!collapsed) {
    event.returnValue = true
    return
  }
  setIgnore(!!ignore)
  event.returnValue = true
})

ipcMain.on('drag-start', (event, { screenX, screenY }) => {
  const b = win.getBounds()
  dragOffset = { x: screenX - b.x, y: screenY - b.y }
  if (dragInterval) clearInterval(dragInterval)
  dragInterval = setInterval(() => {
    const c = screen.getCursorScreenPoint()
    win.setPosition(Math.round(c.x - dragOffset.x), Math.round(c.y - dragOffset.y))
  }, 8)
})

ipcMain.on('drag-stop', () => {
  if (dragInterval) { clearInterval(dragInterval); dragInterval = null }
})

ipcMain.handle('mic-start', async () => {
  const id = ++micReq
  const op = (async () => {
    await loadCaptureWindow()
    if (!captureWin || captureWin.isDestroyed()) {
      return { ok: false, name: 'Error', message: 'mic capture window missing' }
    }
    const started = new Promise((resolve) => {
      const onOk = (_e, req) => {
        if (req !== id) return
        cleanup()
        resolve({ ok: true })
      }
      const onFail = (_e, req, err) => {
        if (req !== id) return
        cleanup()
        resolve({
          ok: false,
          name: (err && err.name) || 'Error',
          message: (err && err.message) || 'mic start failed'
        })
      }
      const timer = setTimeout(() => {
        cleanup()
        resolve({ ok: false, name: 'TimeoutError', message: 'mic start timed out' })
      }, 15000)
      function cleanup() {
        clearTimeout(timer)
        ipcMain.removeListener('mic:started', onOk)
        ipcMain.removeListener('mic:start-failed', onFail)
      }
      ipcMain.on('mic:started', onOk)
      ipcMain.on('mic:start-failed', onFail)
    })
    captureWin.webContents.send('mic:start', id)
    capturing = true
    const result = await started
    if (!result.ok) capturing = false
    ensureOverlayVisible('after-mic-start')
    return result
  })()
  micOp = op.then(() => {}, () => {})
  return op
})

ipcMain.handle('mic-stop', async () => {
  const id = micReq
  await micOp
  if (!captureWin || captureWin.isDestroyed()) return null
  const stopped = new Promise((resolve) => {
    const timer = setTimeout(() => {
      ipcMain.removeListener('mic:stopped', onStop)
      resolve(null)
    }, 5000)
    function onStop(_e, req, buffer) {
      if (req !== id) return
      clearTimeout(timer)
      ipcMain.removeListener('mic:stopped', onStop)
      resolve(buffer || null)
    }
    ipcMain.on('mic:stopped', onStop)
  })
  captureWin.webContents.send('mic:stop', id)
  const buffer = await stopped
  capturing = false
  ensureOverlayVisible('after-mic-stop')
  if (!buffer) return null
  return Buffer.isBuffer(buffer) ? buffer : Buffer.from(buffer)
})

ipcMain.on('microphone-status', (event) => {
  if (process.platform !== 'darwin') {
    event.returnValue = { granted: true, status: 'granted' }
    return
  }
  const status = systemPreferences.getMediaAccessStatus('microphone')
  event.returnValue = { granted: status === 'granted', status }
})

ipcMain.handle('ask-microphone', async () => {
  if (process.platform !== 'darwin') {
    return { granted: true, prompted: false, status: 'granted' }
  }
  const status = systemPreferences.getMediaAccessStatus('microphone')
  if (status === 'granted') {
    return { granted: true, prompted: false, status }
  }
  if (status === 'not-determined') {
    const granted = await systemPreferences.askForMediaAccess('microphone')
    return { granted: !!granted, prompted: true, status: granted ? 'granted' : 'denied' }
  }
  return { granted: false, prompted: false, status }
})

app.whenReady().then(createWindow)
