package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.service.RuntimeCleanupService;
import com.grape.admin.service.RuntimeCredentialService;
import com.grape.admin.service.RuntimeDeviceService;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import javax.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.Map;

/**
 * 运行时管理（设备/引擎/凭据/任务）。直读共享库；凭据与清理为受控写操作（审计）。
 */
@RestController
@RequestMapping("/api/admin")
public class RuntimeController {

    private final RuntimeDeviceService deviceService;
    private final RuntimeCredentialService credentialService;
    private final RuntimeCleanupService cleanupService;

    public RuntimeController(RuntimeDeviceService deviceService,
                             RuntimeCredentialService credentialService,
                             RuntimeCleanupService cleanupService) {
        this.deviceService = deviceService;
        this.credentialService = credentialService;
        this.cleanupService = cleanupService;
    }

    @GetMapping("/devices")
    public Map<String, Object> devices(@RequestParam(required = false) String status,
                                       @RequestParam(required = false) String name,
                                       @RequestParam(defaultValue = "0") int page,
                                       @RequestParam(defaultValue = "10") int size) {
        return deviceService.listDevices(status, name, page, size);
    }

    @GetMapping("/devices/{cid}")
    public Map<String, Object> deviceDetail(@PathVariable Integer cid) {
        return deviceService.deviceDetail(cid);
    }

    @GetMapping("/engines")
    public List<Map<String, Object>> engines() {
        return deviceService.engineOverview();
    }

    @GetMapping("/tasks")
    public List<Map<String, Object>> tasks(@RequestParam(required = false) Integer device,
                                           @RequestParam(required = false) String status,
                                           @RequestParam(defaultValue = "50") int limit) {
        return deviceService.listTasks(device, status, limit);
    }

    @GetMapping("/credentials")
    public List<Map<String, Object>> credentials(@RequestParam(required = false) String scope,
                                                 @RequestParam(required = false) String owner) {
        return deviceService.listCredentials(scope, owner);
    }

    @PostMapping("/credentials")
    public Map<String, String> issueCredential(@RequestBody Map<String, String> body, HttpServletRequest request) {
        String token = credentialService.issue(body.get("deviceName"), AuthContext.username(request));
        return Map.of("token", token, "deviceName", body.get("deviceName") == null ? "" : body.get("deviceName"));
    }

    @PostMapping("/devices/{cid}/revoke")
    public Map<String, String> revoke(@PathVariable Integer cid, HttpServletRequest request) {
        credentialService.revoke(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @PostMapping("/devices/{cid}/cleanup")
    public Map<String, String> cleanupDevice(@PathVariable Integer cid, HttpServletRequest request) {
        cleanupService.cleanupDevice(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @DeleteMapping("/engines/{id}")
    public Map<String, String> deleteEngine(@PathVariable Integer id, HttpServletRequest request) {
        cleanupService.deleteEngine(id, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @PostMapping("/tasks/cleanup")
    public Map<String, String> cleanupTasks(@RequestBody Map<String, Object> body, HttpServletRequest request) {
        @SuppressWarnings("unchecked")
        List<String> statuses = (List<String>) body.get("statuses");
        cleanupService.cleanupTasks(statuses, (String) body.get("before"), AuthContext.username(request));
        return Map.of("status", "ok");
    }
}
