// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// mic_sink — AgentTouch host helper (mic-blackhole branch).
// Build: swiftc -O -o ~/.agentpet/mic_sink host/mic_sink.swift
//
//   list            devices: id, name, in/out channel counts
//   input           print the current default input device name
//   input <name>    make <name> the default input; prints the previous device name
//   play <name>     read s16le 16 kHz mono PCM on stdin and play it to output device
//                   <name>. Pointed at BlackHole, that turns the stream into a
//                   microphone the input method can dictate from. Prints
//                   "ready ..." once the engine runs, a stats line on EOF.
//
// Env MIC_SINK_PRIME_MS (default 120): the ring pre-buffers this much before
// playing so 40 ms network frames don't gap; re-primes after an underrun.
import Foundation
import AVFoundation
import CoreAudio

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

func addr(_ sel: AudioObjectPropertySelector,
          _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    return AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func allDevices() -> [AudioDeviceID] {
    var a = addr(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func deviceName(_ id: AudioDeviceID) -> String {
    var a = addr(kAudioObjectPropertyName)
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, &name) == noErr else { return "" }
    return name as String
}

func channels(_ id: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var a = addr(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, raw) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func findDevice(_ name: String) -> AudioDeviceID? {
    let want = name.lowercased()
    let ids = allDevices()
    if let id = ids.first(where: { deviceName($0).lowercased() == want }) { return id }
    return ids.first(where: { deviceName($0).lowercased().contains(want) })
}

func defaultInput() -> AudioDeviceID {
    var a = addr(kAudioHardwarePropertyDefaultInputDevice)
    var id = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &id)
    return id
}

func setDefaultInput(_ id: AudioDeviceID) -> Bool {
    var a = addr(kAudioHardwarePropertyDefaultInputDevice)
    var v = id
    return AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil,
                                      UInt32(MemoryLayout<AudioDeviceID>.size), &v) == noErr
}

func nominalRate(_ id: AudioDeviceID) -> Double {
    var a = addr(kAudioDevicePropertyNominalSampleRate)
    var rate: Float64 = 0
    var size = UInt32(MemoryLayout<Float64>.size)
    AudioObjectGetPropertyData(id, &a, 0, nil, &size, &rate)
    return rate > 0 ? rate : 48000
}

// Single-producer (stdin thread) / single-consumer (render thread) float ring
// holding DEVICE-rate mono samples (the stdin thread resamples 16 kHz -> device).
final class Ring {
    private var buf: [Float]
    private var head = 0, tail = 0, count = 0
    private let lock = NSLock()
    private var primed = false
    let prime: Int
    var overruns = 0, underruns = 0, pushed = 0, pulled = 0
    var fill: Int { lock.lock(); defer { lock.unlock() }; return count }
    init(_ n: Int, prime: Int) { buf = [Float](repeating: 0, count: n); self.prime = prime }
    func push(_ x: [Float]) {
        lock.lock()
        for v in x {
            if count == buf.count { tail = (tail + 1) % buf.count; count -= 1; overruns += 1 }
            buf[head] = v; head = (head + 1) % buf.count; count += 1
        }
        pushed += x.count
        lock.unlock()
    }
    // interleaved stereo out: the same sample on L and R
    func pullStereo(into p: UnsafeMutablePointer<Float>, _ n: Int) {
        lock.lock()
        if !primed && count >= prime { primed = true }
        for i in 0..<n {
            var v: Float = 0
            if primed && count > 0 {
                v = buf[tail]; tail = (tail + 1) % buf.count; count -= 1
            } else if primed { underruns += 1; primed = false }
            p[i * 2] = v; p[i * 2 + 1] = v
        }
        pulled += n
        lock.unlock()
    }
}

final class Sink {
    let ring: Ring
    init(ring: Ring) { self.ring = ring }
}

// AUHAL render: pull device-rate stereo from the ring. A plain HAL output
// unit pinned to one device — unlike AVAudioEngine it never re-routes
// itself to the system default output when a default device changes.
let renderProc: AURenderCallback = { refCon, _, _, _, nFrames, ioData in
    let sink = Unmanaged<Sink>.fromOpaque(refCon).takeUnretainedValue()
    guard let ioData = ioData else { return noErr }
    let abl = UnsafeMutableAudioBufferListPointer(ioData)
    if let p = abl[0].mData?.assumingMemoryBound(to: Float.self) {
        sink.ring.pullStereo(into: p, Int(nFrames))
    }
    return noErr
}

func play(_ devName: String) {
    guard let dev = findDevice(devName) else { fail("no output device named \(devName)") }
    let devRate = nominalRate(dev)
    let primeMs = Int(ProcessInfo.processInfo.environment["MIC_SINK_PRIME_MS"] ?? "") ?? 120
    let ring = Ring(Int(devRate) * 4, prime: Int(devRate) * primeMs / 1000)
    let sink = Sink(ring: ring)

    var desc = AudioComponentDescription(componentType: kAudioUnitType_Output,
                                         componentSubType: kAudioUnitSubType_HALOutput,
                                         componentManufacturer: kAudioUnitManufacturer_Apple,
                                         componentFlags: 0, componentFlagsMask: 0)
    guard let comp = AudioComponentFindNext(nil, &desc) else { fail("no HAL output unit") }
    var unitOpt: AudioUnit? = nil
    guard AudioComponentInstanceNew(comp, &unitOpt) == noErr, let unit = unitOpt else { fail("cannot create HAL output unit") }
    var one: UInt32 = 1, zero: UInt32 = 0
    AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Output, 0, &one, 4)
    AudioUnitSetProperty(unit, kAudioOutputUnitProperty_EnableIO, kAudioUnitScope_Input, 1, &zero, 4)
    var devID = dev
    guard AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                               &devID, UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr else { fail("cannot pin output device") }
    var asbd = AudioStreamBasicDescription(mSampleRate: devRate, mFormatID: kAudioFormatLinearPCM,
                                           mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
                                           mBytesPerPacket: 8, mFramesPerPacket: 1, mBytesPerFrame: 8,
                                           mChannelsPerFrame: 2, mBitsPerChannel: 32, mReserved: 0)
    guard AudioUnitSetProperty(unit, kAudioUnitProperty_StreamFormat, kAudioUnitScope_Input, 0, &asbd,
                               UInt32(MemoryLayout<AudioStreamBasicDescription>.size)) == noErr else { fail("cannot set stream format") }
    var cb = AURenderCallbackStruct(inputProc: renderProc, inputProcRefCon: Unmanaged.passUnretained(sink).toOpaque())
    guard AudioUnitSetProperty(unit, kAudioUnitProperty_SetRenderCallback, kAudioUnitScope_Input, 0, &cb,
                               UInt32(MemoryLayout<AURenderCallbackStruct>.size)) == noErr else { fail("cannot set render callback") }
    guard AudioUnitInitialize(unit) == noErr else { fail("AudioUnitInitialize failed") }
    guard AudioOutputUnitStart(unit) == noErr else { fail("AudioOutputUnitStart failed") }
    func currentOut() -> AudioDeviceID {
        var d = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &d, &size)
        return d
    }
    print("ready \(deviceName(dev)) out=\(deviceName(currentOut())) device_rate=\(Int(devRate)) prime_ms=\(primeMs) unit=AUHAL")
    fflush(stdout)
    if let logPath = ProcessInfo.processInfo.environment["MIC_SINK_LOG"] {   // 1 Hz status for diagnosis
        Thread.detachNewThread {
            while true {
                let line = "pid=\(getpid()) out=\(deviceName(currentOut())) pushed=\(ring.pushed) pulled=\(ring.pulled) fill=\(ring.fill) fill_ms=\(ring.fill * 1000 / Int(devRate)) overruns=\(ring.overruns) underruns=\(ring.underruns) t=\(Int(Date().timeIntervalSince1970))\n"
                try? line.write(toFile: logPath, atomically: true, encoding: .utf8)
                Thread.sleep(forTimeInterval: 1)
            }
        }
    }

    // stdin: s16le 16 kHz mono -> linear-interpolated device rate -> ring
    let stdin = FileHandle.standardInput
    var pending = Data()
    let step = 16000.0 / devRate           // source samples per output sample
    var last: Float = 0
    var frac = 0.0                         // position between `last` and the next source sample
    while true {
        let data = stdin.availableData          // blocks; empty = EOF
        if data.isEmpty { break }
        pending.append(data)
        let n = pending.count / 2
        if n == 0 { continue }
        var out = [Float]()
        out.reserveCapacity(Int(Double(n) / step) + 2)
        pending.withUnsafeBytes { raw in
            for i in 0..<n {
                let lo = Int(raw[i * 2]), hi = Int(raw[i * 2 + 1])
                let s = Float(Int16(bitPattern: UInt16(truncatingIfNeeded: (hi << 8) | lo))) / 32768.0
                while frac < 1.0 {
                    out.append(last + (s - last) * Float(frac))
                    frac += step
                }
                frac -= 1.0
                last = s
            }
        }
        ring.push(out)
        pending.removeFirst(n * 2)
    }
    // EOF: let what is buffered play out (≤ 8 s) before tearing down
    var waited = 0
    while ring.fill > 0 && waited < 800 { usleep(10_000); waited += 1 }
    AudioOutputUnitStop(unit)
    AudioUnitUninitialize(unit)
    AudioComponentInstanceDispose(unit)
    print("done pushed=\(ring.pushed) pulled=\(ring.pulled) overruns=\(ring.overruns) underruns=\(ring.underruns) drain_ms=\(waited * 10)")
}

// rec <input device> <seconds> <out.wav>: capture what an input device delivers
// (16 kHz mono s16le WAV) — pointed at BlackHole it records exactly what the
// input method would hear from our play stream.
func rec(_ devName: String, _ seconds: Double, _ outPath: String) {
    guard let dev = findDevice(devName) else { fail("no input device named \(devName)") }
    let engine = AVAudioEngine()
    let inp = engine.inputNode
    var devID = dev
    let st = AudioUnitSetProperty(inp.audioUnit!, kAudioOutputUnitProperty_CurrentDevice,
                                  kAudioUnitScope_Global, 0, &devID, UInt32(MemoryLayout<AudioDeviceID>.size))
    guard st == noErr else { fail("cannot select input device (\(st))") }
    let inFmt = inp.outputFormat(forBus: 0)
    let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    guard let conv = AVAudioConverter(from: inFmt, to: outFmt) else { fail("no converter") }
    let file: AVAudioFile
    do { file = try AVAudioFile(forWriting: URL(fileURLWithPath: outPath), settings: outFmt.settings,
                                commonFormat: .pcmFormatInt16, interleaved: true) } catch { fail("open \(outPath): \(error)") }
    var frames: Int64 = 0
    inp.installTap(onBus: 0, bufferSize: 4096, format: inFmt) { buf, _ in
        let cap = AVAudioFrameCount(Double(buf.frameLength) * 16000.0 / inFmt.sampleRate) + 16
        guard let out = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
        var consumed = false
        var err: NSError? = nil
        conv.convert(to: out, error: &err) { _, status in
            if consumed { status.pointee = .noDataNow; return nil }
            consumed = true; status.pointee = .haveData; return buf
        }
        if err == nil && out.frameLength > 0 { try? file.write(from: out); frames += Int64(out.frameLength) }
    }
    do { try engine.start() } catch { fail("engine start: \(error)") }
    print("recording \(deviceName(dev)) rate=\(Int(inFmt.sampleRate)) ch=\(inFmt.channelCount) for \(seconds)s")
    fflush(stdout)
    Thread.sleep(forTimeInterval: seconds)
    inp.removeTap(onBus: 0)
    engine.stop()
    print("done frames=\(frames) -> \(outPath)")
}

let args = CommandLine.arguments
switch args.count > 1 ? args[1] : "" {
case "list":
    for id in allDevices() {
        print("\(id)\t\(deviceName(id))\tin=\(channels(id, kAudioObjectPropertyScopeInput))\tout=\(channels(id, kAudioObjectPropertyScopeOutput))")
    }
case "input":
    let prev = defaultInput()
    if args.count > 2 {
        guard let dev = findDevice(args[2]) else { fail("no input device named \(args[2])") }
        guard setDefaultInput(dev) else { fail("cannot set default input") }
    }
    print(deviceName(prev))
case "play":
    guard args.count > 2 else { fail("usage: mic_sink play <output device name>") }
    play(args[2])
case "rec":
    guard args.count > 4, let secs = Double(args[3]) else { fail("usage: mic_sink rec <input device> <seconds> <out.wav>") }
    rec(args[2], secs, args[4])
default:
    fail("usage: mic_sink list | input [name] | play <name> | rec <name> <seconds> <out.wav>")
}
