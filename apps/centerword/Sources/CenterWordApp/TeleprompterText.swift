import Foundation

struct TeleprompterText {
    static func words(in text: String) -> [String] {
        text
            .split(whereSeparator: \.isWhitespace)
            .map(String.init)
    }
}
