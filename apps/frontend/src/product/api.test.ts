// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiBaseUrl, productApi } from './api'

afterEach(() => { Reflect.deleteProperty(window, '__VOICE_APPLICATION__'); vi.unstubAllGlobals() })

describe('Product API（产品接口）地址', () => {
  it('拒绝缺失的 Host 地址，不回退到本机默认端口', () => {
    expect(() => apiBaseUrl()).toThrow('应用宿主尚未提供语音服务接口地址')
  })

  it('配置缺失时业务请求在触碰网络之前失败', async () => {
    const fetchRequest = vi.fn()
    vi.stubGlobal('fetch', fetchRequest)
    await expect(productApi.snapshot()).rejects.toThrow('应用宿主尚未提供语音服务接口地址')
    expect(fetchRequest).not.toHaveBeenCalled()
  })

  it('只采用 Host 配置并移除末尾斜杠', () => {
    window.__VOICE_APPLICATION__ = {
      managed: true,
      origin: 'http://127.0.0.1:42123',
      voice_api_url: 'http://127.0.0.1:43876/',
      voice_ws_url: 'ws://127.0.0.1:43876/ws',
    }
    expect(apiBaseUrl()).toBe('http://127.0.0.1:43876')
  })
})

describe('主机配对接口回执', () => {
  it('静态页面的 HTML 响应不能当作配对 JSON，也不向用户回显页面内容', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<!doctype html><title>管理页</title>', {
      status: 200, headers: { 'Content-Type': 'text/html; charset=utf-8' },
    })))
    await expect(productApi.devicePairingStatus()).rejects.toThrow('主机配对接口尚未就绪')
  })

  it('配对操作收到损坏 JSON 时返回可理解的错误', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response('{broken', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    })))
    await expect(productApi.openDevicePairing(120)).rejects.toThrow('主机配对接口返回了无效数据')
    await expect(productApi.closeDevicePairing()).rejects.toThrow('主机配对接口返回了无效数据')
  })
})
