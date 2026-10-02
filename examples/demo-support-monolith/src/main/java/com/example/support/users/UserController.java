package com.example.support.users;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/users")
public class UserController {
    private final UserDirectory users;

    public UserController(UserDirectory users) { this.users = users; }

    @GetMapping("/demo-user")
    public SupportUser demoUser() { return users.requireActive("demo-user"); }
}
