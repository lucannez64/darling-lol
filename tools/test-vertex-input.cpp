#include <indium/vertex-input.private.hpp>
#include <cassert>
#include <cstdio>
#include <stdexcept>

int main() {
    using namespace Indium;
    VertexDescriptor descriptor;
    descriptor.layouts[30] = {16, VertexStepFunction::PerVertex, 1};
    descriptor.attributes[0] = {VertexFormat::Float4, 0, 30};
    descriptor.layouts[29] = {4, VertexStepFunction::Constant, 0};
    descriptor.attributes[7] = {VertexFormat::UChar4Normalized, 0, 29};
    descriptor.layouts[4] = {24, VertexStepFunction::PerVertex, 1};
    descriptor.attributes[2] = {VertexFormat::Float3, 0, 4};
    descriptor.attributes[3] = {VertexFormat::Float2, 12, 4};
    descriptor.layouts[9] = {8, VertexStepFunction::PerInstance, 1};
    descriptor.attributes[5] = {VertexFormat::Float2, 0, 9};

    // A vertex-ID fullscreen shader requires no bound vertex buffers even when
    // a shared descriptor has unused inputs at high Metal buffer indices.
    auto fullscreen = vertexInputPlan(descriptor, {});
    assert(fullscreen.bindings.empty() && fullscreen.attributes.empty() && fullscreen.metalBindings.empty());
    auto plan = vertexInputPlan(descriptor, {7, 2, 3, 5});
    assert((plan.metalBindings == std::vector<size_t>{29, 4, 9}));
    assert(plan.bindings.size() == 3 && plan.attributes.size() == 4);
    assert(plan.bindings[0].stride == 0 && plan.bindings[0].inputRate == VK_VERTEX_INPUT_RATE_VERTEX);
    assert(plan.attributes[0].format == VK_FORMAT_R8G8B8A8_UNORM);
    assert(plan.bindings[1].stride == 24 && plan.bindings[1].inputRate == VK_VERTEX_INPUT_RATE_VERTEX);
    assert(plan.bindings[2].stride == 8 && plan.bindings[2].inputRate == VK_VERTEX_INPUT_RATE_INSTANCE);
    assert(plan.attributes[1].binding == plan.attributes[2].binding && plan.attributes[2].offset == 12);
    assert(plan.attributes[0].location == 7 && plan.attributes[3].location == 5);
    auto rejects = [&](const std::vector<size_t>& inputs) {
        try { (void)vertexInputPlan(descriptor, inputs); } catch (const std::exception&) { return; }
        assert(false && "invalid active input was accepted");
    };
    rejects({8}); // Missing attributes must not silently map to Vulkan binding 0.
    descriptor.layouts.erase(29);
    rejects({7});
    descriptor.layouts[29] = {4, VertexStepFunction::Constant, 0};
    descriptor.layouts[30].stepRate = 2;
    (void)vertexInputPlan(descriptor, {7}); // An unused unsupported layout is ignored.
    rejects({0});
    std::puts("vertex-input plan: all checks passed");
}
