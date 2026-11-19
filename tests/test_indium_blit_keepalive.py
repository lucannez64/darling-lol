"""Blit operands must live until the command buffer completes (#252).

Metal retains the buffers and textures a blit uses, so callers may release them right after encoding. Indium has to
hold them too, or the GPU copies from or into memory that was already freed.
"""
import re
import unittest
import test_runtime_compatibility as sources

PATH = 'src/indium/blit-command-encoder.cpp'
OPERAND = re.compile(r'const std::shared_ptr<(?:Buffer|Texture)>& (\w+)')


class BlitKeepAlive(unittest.TestCase):
    @staticmethod
    def body(source, opening):
        depth, end = 1, opening + 1
        while depth:
            depth += (source[end] == '{') - (source[end] == '}')
            end += 1
        return source[opening:end]

    def methods(self):
        source = sources.CompatibilityChecks.patched(self, 'metal--deps--indium', PATH)
        found = []
        for match in re.finditer(r'^void Indium::PrivateBlitCommandEncoder::(\w+)\(([^)]*)\)\s*\{', source, re.M):
            body = self.body(source, match.end() - 1)
            found.append((match.group(1), OPERAND.findall(match.group(2)), body))
        return found

    def test_every_operand_is_held(self):
        # methods that only forward to another copy() are covered by the one that records the command
        recording = [(name, operands, body) for name, operands, body in self.methods() if operands and 'vkCmd' in body]
        self.assertGreaterEqual(len(recording), 6)
        for name, operands, body in recording:
            with self.subTest(method=name, operands=operands):
                for operand in operands:
                    self.assertIn(f'_keepAlive.push_back({operand});', body)

if __name__ == '__main__':
    unittest.main()
