// A process that is not the gray session's target loads the layer (the session's
// debug.vulkan.layers is global): the layer must behave as if it were not there.
// Run with argv[0] = the process name to simulate, argv[1] = debug.knobs.target,
// argv[2] = "transparent" | "target". The harness generates layer_vk_names.h
// (every vk* name in the layer sources) and layer_knob_props.h (every debug.knobs.*).
#include "../layer/vk_layer_refknobs.cpp"
#include "layer_knob_props.h"
#include "layer_vk_names.h"
#include <cstdlib>
#include <cstring>

static void *h_key(uintptr_t n) { return reinterpret_cast<void *>(n); }
#define CHECK(c, ...) do { if (!(c)) { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); exit(1); } } while (0)

static void *instance_word = h_key(0x1111);
static void *physical_word = h_key(0x1111);   // physical devices share the instance's dispatch key
static void *device_word = h_key(0x2222);
static VkInstance lower_instance = reinterpret_cast<VkInstance>(&instance_word);
static VkPhysicalDevice physical = reinterpret_cast<VkPhysicalDevice>(&physical_word);
static VkDevice lower_device = reinterpret_cast<VkDevice>(&device_word);
static const void *instance_link_seen = nullptr, *device_link_seen = nullptr;
static const void *instance_create_seen = nullptr, *device_create_seen = nullptr;
static unsigned instance_creates = 0, device_creates = 0;

static VkResult VKAPI_CALL lower_CreateInstance(const VkInstanceCreateInfo *ci, const VkAllocationCallbacks *,
                                                VkInstance *out) {
    ++instance_creates;
    instance_create_seen = ci;
    instance_link_seen = reinterpret_cast<const RkInstCreateInfo *>(ci->pNext)->u.pLayerInfo;
    *out = lower_instance;
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL lower_CreateDevice(VkPhysicalDevice, const VkDeviceCreateInfo *ci,
                                              const VkAllocationCallbacks *, VkDevice *out) {
    ++device_creates;
    device_create_seen = ci;
    device_link_seen = reinterpret_cast<const RkDevCreateInfo *>(ci->pNext)->u.pLayerInfo;
    *out = lower_device;
    return VK_SUCCESS;
}
// Every other lower function is a distinct address that is compared, never called.
static PFN_vkVoidFunction lower_named(const char *level, const char *name) {
    static std::map<std::string, uintptr_t> ids;
    std::string k = std::string(level) + name;
    auto it = ids.emplace(k, ids.size() + 1).first;
    return reinterpret_cast<PFN_vkVoidFunction>(0x7f0000000000ull + 16 * it->second);
}
static PFN_vkVoidFunction VKAPI_CALL lower_gipa(VkInstance, const char *name) {
    if (!strcmp(name, "vkCreateInstance")) return reinterpret_cast<PFN_vkVoidFunction>(lower_CreateInstance);
    if (!strcmp(name, "vkCreateDevice")) return reinterpret_cast<PFN_vkVoidFunction>(lower_CreateDevice);
    return lower_named("i:", name);
}
static PFN_vkVoidFunction VKAPI_CALL lower_gdpa(VkDevice, const char *name) { return lower_named("d:", name); }

static bool is(const char *a, const char *b) { return !strcmp(a, b); }
template <typename F> static PFN_vkVoidFunction fn(F f) { return reinterpret_cast<PFN_vkVoidFunction>(f); }

int main(int argc, char **argv) {
    CHECK(argc == 3, "usage: <argv0=process> <debug.knobs.target> transparent|target");
    const bool want_transparent = is(argv[2], "transparent");
    for (const char *p : kLayerKnobProps) rk_host_props[p] = "1";
    rk_host_props["debug.knobs.target"] = argv[1];

    // The loader's view: negotiate, then ask for vkCreateInstance before any instance exists.
    RkNegotiate neg{};
    neg.loaderLayerInterfaceVersion = 2;
    CHECK(vkNegotiateLoaderLayerInterfaceVersion(&neg) == VK_SUCCESS, "negotiate");
    auto gipa = neg.pfnGetInstanceProcAddr;
    auto create = reinterpret_cast<PFN_vkCreateInstance>(gipa(VK_NULL_HANDLE, "vkCreateInstance"));
    CHECK(create, "no vkCreateInstance");

    RkInstLink below{nullptr, lower_gipa};
    RkInstLink ours{&below, lower_gipa};
    RkInstCreateInfo chain{VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO, nullptr, RK_LAYER_LINK_INFO, {&ours}};
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ici.pNext = &chain;
    VkInstance instance = VK_NULL_HANDLE;
    CHECK(create(&ici, nullptr, &instance) == VK_SUCCESS && instance == lower_instance, "create instance");
    CHECK(instance_creates == 1 && instance_create_seen == &ici && instance_link_seen == &below,
          "next vkCreateInstance must get the caller's create info with the chain advanced");

    unsigned names = 0, forwarded = 0;
    PFN_vkVoidFunction present = gipa(instance, "vkQueuePresentKHR");
    if (want_transparent) {
        CHECK(rk_host_prop_reads.size() == 1 && rk_host_prop_reads[0] == "debug.knobs.target",
              "transparent process read %zu properties (first extra: %s)", rk_host_prop_reads.size(),
              rk_host_prop_reads.size() > 1 ? rk_host_prop_reads[1].c_str() : "-");
        for (const char *n : kLayerVkNames) {
            ++names;
            PFN_vkVoidFunction got = gipa(instance, n), want;
            if (is(n, "vkGetInstanceProcAddr")) want = fn(dispatch_instance);
            else if (is(n, "vkCreateInstance")) want = fn(rk_CreateInstance);
            else if (is(n, "vkCreateDevice")) want = fn(rk_CreateDevice);
            else if (is(n, "vkGetDeviceProcAddr")) want = fn(dispatch_device);
            else { want = lower_gipa(instance, n); ++forwarded; }
            CHECK(got == want, "instance-level %s is not forwarded to the next layer", n);
        }
        auto create_device = reinterpret_cast<PFN_vkCreateDevice>(gipa(instance, "vkCreateDevice"));
        RkDevLink dbelow{nullptr, lower_gipa, lower_gdpa};
        RkDevLink dours{&dbelow, lower_gipa, lower_gdpa};
        RkDevCreateInfo dchain{VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO, nullptr, RK_LAYER_LINK_INFO, {&dours}};
        VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
        dci.pNext = &dchain;
        VkDevice device = VK_NULL_HANDLE;
        CHECK(create_device(physical, &dci, nullptr, &device) == VK_SUCCESS && device == lower_device, "create device");
        CHECK(device_creates == 1 && device_create_seen == &dci && device_link_seen == &dbelow,
              "next vkCreateDevice must get the caller's create info with the chain advanced");
        auto gdpa = neg.pfnGetDeviceProcAddr;
        for (const char *n : kLayerVkNames) {
            PFN_vkVoidFunction got = gdpa(device, n);
            PFN_vkVoidFunction want = is(n, "vkGetDeviceProcAddr") ? fn(dispatch_device) : lower_gdpa(device, n);
            CHECK(got == want, "device-level %s is not forwarded to the next layer", n);
        }
        CHECK(rk_host_prop_reads.size() == 1, "properties read after instance creation");
        CHECK(rk_host_log_lines == 0 && rk_host_marker.empty(), "transparent process logged %u lines", rk_host_log_lines);
        CHECK(!fd_on() && !ip_on() && !sr_on() && !g_dump_passes && !g_frameaudit && !g_upop && !g_npuop,
              "a knob was switched on in a transparent process");
    } else {
        CHECK(rk_host_prop_reads.size() > 1, "target process must read its knobs");
        CHECK(present && present != lower_gipa(instance, "vkQueuePresentKHR"), "target process must hook vkQueuePresentKHR");
        CHECK(fd_on() && g_dump_passes, "target process must apply its knobs");
    }
    printf("{\"transparent\":%s,\"process\":\"%s\",\"vk_names\":%u,\"forwarded\":%u,\"prop_reads\":%zu}\n",
           want_transparent ? "true" : "false", self_process().c_str(), names, forwarded, rk_host_prop_reads.size());
}
