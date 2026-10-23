// Bridges a paired Unicorn's Bluetooth serial channel to a local TCP port.
//
//   RhinoConnect <name-or-address> <tcp-port>
//
// On current macOS the /dev/cu.UN-* port of a serial-only (SPP) device
// does not reliably carry its data, so this opens the RFCOMM channel
// through IOBluetooth and relays bytes both ways to the one client that
// connects to 127.0.0.1:<tcp-port>. It exits when the client disconnects
// or the channel closes. It is an app bundle because IOBluetooth needs a
// Bluetooth usage description, which a bare interpreter lacks.
//
// Status lines on stdout: LISTENING, CONNECTED, CLOSED, or ERROR <why>.
import Foundation
import IOBluetooth

func emit(_ line: String) {
    print(line)
    fflush(stdout)
}

func fail(_ why: String) -> Never {
    emit("ERROR \(why)")
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 3, let tcpPort = UInt16(args[2]) else {
    emit("usage: RhinoConnect <name-or-address> <tcp-port>")
    exit(2)
}
let target = args[1]

// --- local TCP server, one client -------------------------------------

let server = socket(AF_INET, SOCK_STREAM, 0)
var yes: Int32 = 1
setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
var address = sockaddr_in()
address.sin_family = sa_family_t(AF_INET)
address.sin_port = tcpPort.bigEndian
address.sin_addr.s_addr = inet_addr("127.0.0.1")
let bound = withUnsafePointer(to: &address) {
    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        bind(server, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
    }
}
guard bound == 0, listen(server, 1) == 0 else { fail("cannot listen on \(tcpPort)") }
emit("LISTENING \(tcpPort)")

// --- the headset's RFCOMM channel ---------------------------------------

final class Bridge: NSObject, IOBluetoothRFCOMMChannelDelegate {
    var client: Int32 = -1

    func rfcommChannelData(_ channel: IOBluetoothRFCOMMChannel!,
                           data dataPointer: UnsafeMutableRawPointer!,
                           length dataLength: Int) {
        var offset = 0
        while offset < dataLength {
            let sent = send(client, dataPointer + offset, dataLength - offset, 0)
            if sent <= 0 { finish("client gone") }
            offset += sent
        }
    }

    func rfcommChannelClosed(_ channel: IOBluetoothRFCOMMChannel!) {
        finish("channel closed")
    }
}

var channel: IOBluetoothRFCOMMChannel?

func finish(_ why: String) -> Never {
    channel?.close()
    channel?.getDevice()?.closeConnection()
    emit("CLOSED \(why)")
    exit(0)
}

// Matched by address, or by name ignoring dots: the serial port of
// "UN-2024.08.41" is named UN-20240841.
func plain(_ name: String) -> String {
    name.lowercased().replacingOccurrences(of: ".", with: "")
}
let paired = (IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice]) ?? []
let wanted = target.lowercased().replacingOccurrences(of: ":", with: "-")
guard let device = paired.first(where: {
    plain($0.name ?? "") == plain(target) || ($0.addressString ?? "").lowercased() == wanted
}) else { fail("not paired: \(target)") }

/// Runs the main run loop until done() is true or timeout passes.
func spin(_ timeout: TimeInterval, until done: () -> Bool) {
    let deadline = Date(timeIntervalSinceNow: timeout)
    while !done() && Date() < deadline {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.1))
    }
}

if !device.isConnected() {
    let status = device.openConnection()
    if status != kIOReturnSuccess {
        fail(String(format: "openConnection 0x%08x", status))
    }
}

let auth = device.requestAuthentication()
emit(String(format: "AUTH 0x%08x connected=%d", auth, device.isConnected() ? 1 : 0))

// The serial port service's channel number, from a fresh SDP query; the
// query is asynchronous, so wait for its answer.
final class SDPWait: NSObject {
    var finished = false
    @objc func sdpQueryComplete(_ device: IOBluetoothDevice!, status: IOReturn) {
        finished = true
    }
}
let sdp = SDPWait()
if device.performSDPQuery(sdp) == kIOReturnSuccess {
    spin(10) { sdp.finished }
}
var channelID: BluetoothRFCOMMChannelID = 0
for record in (device.services as? [IOBluetoothSDPServiceRecord]) ?? [] {
    var id: BluetoothRFCOMMChannelID = 0
    let hasChannel = record.getRFCOMMChannelID(&id) == kIOReturnSuccess
    emit("SERVICE \(record.getServiceName() ?? "?")"
         + (hasChannel ? " rfcomm \(id)" : ""))
    if hasChannel && channelID == 0 { channelID = id }
}
if channelID == 0 { channelID = 1 }

let bridge = Bridge()

// Accept off the main thread: IOBluetooth delivers on the main run loop.
DispatchQueue.global().async {
    let client = accept(server, nil, nil)
    guard client >= 0 else { fail("accept failed") }
    DispatchQueue.main.async {
        bridge.client = client
        var opened: IOBluetoothRFCOMMChannel?
        var status = kIOReturnError
        for _ in 0..<6 {
            status = device.openRFCOMMChannelSync(&opened, withChannelID: channelID,
                                                  delegate: bridge)
            if status == kIOReturnSuccess { break }
            emit(String(format: "RETRY openRFCOMMChannel %d: 0x%08x", channelID, status))
            spin(1) { false }
        }
        guard status == kIOReturnSuccess, let opened = opened else {
            fail(String(format: "openRFCOMMChannel %d: 0x%08x", channelID, status))
        }
        channel = opened
        emit("CONNECTED \(device.addressString ?? "") channel \(channelID)")

        // Client to headset: read on a thread, write on the main thread.
        DispatchQueue.global().async {
            var buffer = [UInt8](repeating: 0, count: 1024)
            while true {
                let n = recv(client, &buffer, buffer.count, 0)
                if n <= 0 { DispatchQueue.main.async { finish("client closed") }; return }
                let chunk = Array(buffer[0..<n])
                DispatchQueue.main.async {
                    var bytes = chunk
                    _ = opened.writeSync(&bytes, length: UInt16(bytes.count))
                }
            }
        }
    }
}

RunLoop.main.run()
